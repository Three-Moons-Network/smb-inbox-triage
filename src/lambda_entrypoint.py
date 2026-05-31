# -*- coding: utf-8 -*-
"""
AWS Lambda entry point — classifier function.

Handles API Gateway v2 (HTTP API) proxy events.

    POST /webhook  — receives an email payload, classifies it, stores the
                     result to DynamoDB, publishes an EmailClassified event
                     to EventBridge, and optionally notifies Slack.

Import-path note
----------------
Terraform's ``archive_file`` datasource zips the *contents* of ``src/``
directly (``source_dir = "...src"``), so at runtime the Lambda zip root
contains:

    classifier/   adapters/   feedback/   observability/   router/

The rest of the codebase uses ``from src.xxx import`` (natural for local dev
where ``src/`` is a sub-directory under the project root).  Lambda adds the
zip root to sys.path, so there is no ``src`` package unless we create one.
The block below injects a namespace-module pointing ``src`` at the zip root,
satisfying those imports without touching any other file.

This technique is identical to the one used in src/main.py (GCP entrypoint).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types
from datetime import datetime, timezone

# ── sys.modules alias: make 'from src.xxx import' work at the zip root ────────
#
# Same two-pass strategy as src/main.py — see that file for detailed rationale.
# Pass 1 — leaf modules (stdlib + third-party only, no src.* imports)
import classifier.models      # noqa: E402
import observability.tracing  # noqa: E402

# Pass 2 — create src namespace and alias already-loaded canonical modules
_here = os.path.dirname(os.path.abspath(__file__))
_src_mod = types.ModuleType("src")
_src_mod.__path__ = [_here]   # type: ignore[attr-defined]
_src_mod.__package__ = "src"
sys.modules["src"] = _src_mod

for _src_key, _canon_key in [
    ("src.classifier",            "classifier"),
    ("src.classifier.models",     "classifier.models"),
    ("src.observability",         "observability"),
    ("src.observability.tracing", "observability.tracing"),
]:
    if _canon_key in sys.modules:
        sys.modules[_src_key] = sys.modules[_canon_key]

# Pass 3 — import modules that contain `from src.xxx import` statements.
import classifier.handler                                                    # noqa: E402
sys.modules["src.classifier.handler"] = sys.modules["classifier.handler"]

import adapters.aws_bedrock                                                  # noqa: E402
sys.modules.setdefault("src.adapters", sys.modules.get("adapters"))
sys.modules["src.adapters.aws_bedrock"] = sys.modules["adapters.aws_bedrock"]

import router.rules                                                          # noqa: E402
sys.modules.setdefault("src.router", sys.modules.get("router"))
sys.modules["src.router.rules"] = sys.modules["router.rules"]

import router.destinations                                                   # noqa: E402
sys.modules["src.router.destinations"] = sys.modules["router.destinations"]

# ── Application imports ────────────────────────────────────────────────────────
from classifier.handler import classify, LLMInvocationError  # noqa: E402
from adapters.aws_bedrock import BedrockAdapter               # noqa: E402
from classifier.models import EmailMessage                     # noqa: E402
from router.rules import route                                 # noqa: E402
from router.destinations import dispatch                       # noqa: E402
from observability.tracing import flush as _otel_flush        # noqa: E402

logger = logging.getLogger(__name__)

# ── Cold-start initialisation ─────────────────────────────────────────────────

_adapter: BedrockAdapter | None = None
_cold_start_done = False


def _cold_start_init() -> None:
    """
    Run once per Lambda container lifetime.

    1.  Fetch the Slack webhook URL from Secrets Manager and inject it into the
        environment as SLACK_WEBHOOK_DEFAULT (destinations.py reads this as the
        fallback for any channel that doesn't have its own dedicated env var).
    2.  Instantiate the Bedrock adapter (reused across warm invocations).
    """
    global _adapter, _cold_start_done
    if _cold_start_done:
        return

    # Fetch Slack webhook URL from Secrets Manager
    secret_name = os.environ.get("SLACK_WEBHOOK_SECRET_NAME")
    if secret_name:
        try:
            import boto3  # noqa: PLC0415
            sm = boto3.client(
                "secretsmanager",
                region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"),
            )
            resp = sm.get_secret_value(SecretId=secret_name)
            webhook_url = resp.get("SecretString", "").strip()
            if webhook_url:
                # destinations.py falls back to SLACK_WEBHOOK_DEFAULT for any
                # channel not explicitly configured — sufficient for dev.
                os.environ["SLACK_WEBHOOK_DEFAULT"] = webhook_url
                logger.info("Slack webhook loaded from Secrets Manager")
            else:
                logger.warning("SLACK_WEBHOOK_SECRET_NAME resolved but value is empty")
        except Exception as exc:
            # Non-fatal at cold start — Slack notifications will fail gracefully
            logger.warning("Failed to load Slack webhook from Secrets Manager: %s", exc)

    _adapter = BedrockAdapter()
    _cold_start_done = True


# ── DynamoDB helper ────────────────────────────────────────────────────────────

def _save_to_dynamodb(record) -> None:
    """Persist a ClassificationRecord to DynamoDB classifications table."""
    import boto3  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    table_name = os.environ["DYNAMODB_CLASSIFICATIONS_TABLE"]
    dynamodb   = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"),
    )
    table = dynamodb.Table(table_name)

    # Convert Pydantic model to a plain dict via JSON round-trip so that
    # enum values become strings and nested models become dicts.
    # parse_float=Decimal is required — boto3 DynamoDB does not accept Python
    # floats and raises "Float types are not supported. Use Decimal types instead."
    doc = json.loads(record.model_dump_json(), parse_float=Decimal)

    # ── Hoist GSI key to top level ────────────────────────────────────────────
    #
    # The intent-time-index GSI declares 'intent' as its hash key, which requires
    # it to be a top-level attribute on every DynamoDB item.
    # model_dump_json() serialises intent nested under result.intent — items
    # written without this hoist have no top-level 'intent', so the GSI is
    # never populated and intent appears as None in all queries.
    # classified_at is already a top-level field on ClassificationRecord.
    doc["intent"] = record.result.intent.value

    # TTL: expire the record 90 days from now (Unix epoch seconds)
    doc["expires_at"] = int(
        (datetime.now(timezone.utc).timestamp()) + (90 * 24 * 60 * 60)
    )

    table.put_item(Item=doc)
    logger.debug("DynamoDB write complete — record_id=%s", record.record_id)


# ── EventBridge helper ─────────────────────────────────────────────────────────

def _publish_event(record) -> None:
    """Publish an EmailClassified event to the EventBridge custom bus."""
    import boto3  # noqa: PLC0415

    bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME")
    if not bus_name:
        logger.warning("EVENTBRIDGE_BUS_NAME not set — skipping EventBridge publish")
        return

    events = boto3.client(
        "events",
        region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"),
    )

    detail = {
        "record_id":     record.record_id,
        "intent":        record.result.intent.value,
        "urgency":       record.result.urgency.value,
        "sentiment":     record.result.sentiment.value,
        "confidence":    record.result.confidence,
        "requires_human": record.result.requires_human,
        "summary":       record.result.summary,
        "model_id":      record.model_id,
        "cloud":         record.cloud,
        "latency_ms":    record.latency_ms,
        "classified_at": record.classified_at,
        "email": {
            "message_id":   record.email.message_id,
            "from_address": record.email.from_address,
            "subject":      record.email.subject,
            "source":       record.email.source,
        },
    }

    try:
        events.put_events(
            Entries=[{
                "Source":       "smb-inbox-triage",
                "DetailType":   "EmailClassified",
                "Detail":       json.dumps(detail),
                "EventBusName": bus_name,
            }]
        )
        logger.info(
            "EventBridge event published",
            extra={"record_id": record.record_id, "intent": record.result.intent.value},
        )
    except Exception as exc:
        # Non-fatal — don't fail the webhook response for an event bus error
        logger.warning("EventBridge publish failed: %s", exc)


# ── API Gateway response helper ────────────────────────────────────────────────

def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body),
    }


# ── Lambda handler ─────────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """
    API Gateway v2 HTTP proxy Lambda handler — classifier.

    Accepts a JSON body with email fields and returns a classification result.

    Expected request body::

        {
            "messageId":   "unique-id",
            "fromAddress": "sender@example.com",
            "fromName":    "Sender Name",
            "toAddress":   "inbox@company.com",
            "subject":     "Email subject",
            "bodyText":    "Plain text body",
            "receivedAt":  "2026-01-01T12:00:00Z",
            "source":      "webhook"
        }

    Response codes:
        200 — classified, stored, and event published
        202 — classified but DynamoDB write failed (record preserved in logs)
        400 — malformed request
        405 — wrong HTTP method
        500 — classification schema error
        502 — LLM invocation failed
    """
    _cold_start_init()

    # API Gateway v2 sends method in requestContext.http.method
    method = (
        event.get("requestContext", {})
             .get("http", {})
             .get("method", "")
             .upper()
    )
    if method and method != "POST":
        return _response(405, {"error": "Method Not Allowed"})

    # Parse body — API GW v2 sends it as a string (may be base64-encoded)
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        data: dict = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON body: %s", exc)
        _otel_flush()
        return _response(400, {"error": "Invalid JSON body"})

    # Build EmailMessage
    try:
        email = EmailMessage(
            message_id    = data.get("messageId", "unknown"),
            from_address  = data.get("fromAddress", "unknown@example.com"),
            from_name     = data.get("fromName") or None,
            to_address    = data.get("toAddress") or "",
            subject       = data.get("subject")   or "",
            body_text     = data.get("bodyText")   or "",
            received_at   = data.get("receivedAt") or "",
            source        = data.get("source")     or "webhook",
        )
    except Exception as exc:
        logger.error("Failed to build EmailMessage: %s", exc)
        _otel_flush()
        return _response(400, {"error": f"Invalid email payload: {exc}"})

    # Classify
    try:
        record = classify(email, _adapter)
    except LLMInvocationError as exc:
        logger.error("LLM invocation failed: %s", exc)
        _otel_flush()
        return _response(502, {"error": "LLM invocation failed"})
    except ValueError as exc:
        logger.error("Classification schema error: %s", exc)
        _otel_flush()
        return _response(500, {"error": "Classification failed"})

    # Persist to DynamoDB
    stored = True
    try:
        _save_to_dynamodb(record)
    except Exception as exc:
        stored = False
        logger.error("DynamoDB write failed (record still logged): %s", exc)

    # Publish to EventBridge (non-fatal on failure)
    _publish_event(record)

    # Route + dispatch (non-fatal — Slack/HubSpot/etc. may not be configured)
    try:
        decision = route(record.result)
        dispatch(record, decision)
    except Exception as exc:
        logger.warning("Routing/dispatch failed (non-fatal): %s", exc)

    _otel_flush()

    if not stored:
        return _response(202, {
            "status":    "classified_not_stored",
            "record_id": record.record_id,
        })

    logger.info(
        "Classification complete",
        extra={
            "record_id":  record.record_id,
            "intent":     record.result.intent.value,
            "urgency":    record.result.urgency.value,
            "confidence": record.result.confidence,
            "cloud":      record.cloud,
            "model_id":   record.model_id,
            "latency_ms": record.latency_ms,
        },
    )
    return _response(200, {
        "status":    "ok",
        "record_id": record.record_id,
        "intent":    record.result.intent.value,
        "urgency":   record.result.urgency.value,
        "confidence": record.result.confidence,
        "requires_human": record.result.requires_human,
        "summary":   record.result.summary,
    })
