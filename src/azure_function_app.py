# -*- coding: utf-8 -*-
"""
Azure Functions v2 entry point — classifier function.

Route: POST /api/webhook

Accepts a flat JSON email payload, classifies it via Azure OpenAI, writes
the result to Cosmos DB, and optionally notifies Slack via router/destinations.

Import-path note
----------------
Terraform's zip deploy places the *contents* of the deployment zip at the
function root, so the runtime sees:

    classifier/   adapters/   feedback/   observability/   router/

at the top of sys.path — identical to the Lambda and GCP zips.  The same
two-pass sys.modules alias trick is used here to satisfy `from src.xxx import`
statements without creating duplicate class objects.

Env vars (set via Terraform app_settings + Key Vault references)
-----------------------------------------------------------------
  CLOUD                         = "azure"
  AZURE_OPENAI_ENDPOINT         HTTPS endpoint for Azure OpenAI resource
  AZURE_OPENAI_DEPLOYMENT       Deployment name (e.g. gpt-4.1-mini)
  AZURE_OPENAI_API_VERSION      e.g. 2024-08-01-preview
  AZURE_OPENAI_API_KEY          Resolved from Key Vault at runtime
  COSMOS_CONNECTION_STRING      Cosmos DB account endpoint URL (not a key string;
                                key auth is disabled — Function App MSI used)
  COSMOS_DATABASE               Database name (e.g. inbox-triage)
  COSMOS_CONTAINER_CLASSIFICATIONS  Container name (e.g. classifications)
  SLACK_WEBHOOK_URL             Resolved from Key Vault at runtime
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types

import azure.functions as func

# ── sys.modules alias: make 'from src.xxx import' work at the zip root ────────
# Same two-pass strategy as src/main.py (GCP) and lambda_entrypoint.py (AWS).

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

# Pass 3 — modules that contain `from src.xxx import` statements
import classifier.handler                                                    # noqa: E402
sys.modules["src.classifier.handler"] = sys.modules["classifier.handler"]

import adapters.azure_openai                                                 # noqa: E402
sys.modules.setdefault("src.adapters", sys.modules.get("adapters"))
sys.modules["src.adapters.azure_openai"] = sys.modules["adapters.azure_openai"]

import router.rules                                                          # noqa: E402
sys.modules.setdefault("src.router", sys.modules.get("router"))
sys.modules["src.router.rules"] = sys.modules["router.rules"]

import router.destinations                                                   # noqa: E402
sys.modules["src.router.destinations"] = sys.modules["router.destinations"]

# ── Application imports ────────────────────────────────────────────────────────
from classifier.handler import classify, LLMInvocationError  # noqa: E402
from adapters.azure_openai import AzureOpenAIAdapter          # noqa: E402
from classifier.models import EmailMessage                     # noqa: E402
from router.rules import route                                 # noqa: E402
from router.destinations import dispatch                       # noqa: E402
from observability.tracing import flush as _otel_flush        # noqa: E402

logger = logging.getLogger(__name__)

# ── Azure Functions app ────────────────────────────────────────────────────────

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ── Cold-start initialisation ──────────────────────────────────────────────────

_adapter: AzureOpenAIAdapter | None = None


def _get_adapter() -> AzureOpenAIAdapter:
    """Return a singleton AzureOpenAIAdapter, instantiated on first call."""
    global _adapter
    if _adapter is None:
        _adapter = AzureOpenAIAdapter()
    return _adapter


# ── Cosmos DB helper ───────────────────────────────────────────────────────────

def _save_to_cosmos(record) -> None:
    """Persist a ClassificationRecord to Cosmos DB via Managed Identity."""
    from azure.cosmos import CosmosClient          # noqa: PLC0415
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    # COSMOS_CONNECTION_STRING holds the account endpoint URL, not a key string.
    # Key-based auth is disabled on the Cosmos account (local_authentication_disabled = true).
    # DefaultAzureCredential resolves to the Function App's System-Assigned MSI at runtime.
    endpoint = os.environ["COSMOS_CONNECTION_STRING"]
    client = CosmosClient(endpoint, credential=DefaultAzureCredential())

    container = (
        client
        .get_database_client(os.environ["COSMOS_DATABASE"])
        .get_container_client(os.environ["COSMOS_CONTAINER_CLASSIFICATIONS"])
    )

    doc = json.loads(record.model_dump_json())

    # Cosmos DB requires every document to have a top-level 'id' field (string).
    # ClassificationRecord uses 'record_id' as its identifier; map it here.
    doc["id"] = doc["record_id"]

    # Hoist intent to top-level so the composite index (intent/classified_at)
    # is populated — same requirement as the DynamoDB GSI hoist in lambda_entrypoint.py.
    doc["intent"] = record.result.intent.value

    # TTL — per-document override matches the container default_ttl of 90 days.
    doc["ttl"] = 90 * 24 * 60 * 60

    container.upsert_item(doc)
    logger.debug("Cosmos DB write complete — record_id=%s", record.record_id)


# ── Route handler ──────────────────────────────────────────────────────────────

@app.route(route="webhook", methods=["POST"])
def webhook(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/webhook — classify an inbound email.

    Accepts a flat JSON object (no envelope):
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
        200 — classified and stored
        202 — classified but Cosmos DB write failed (record preserved in logs)
        400 — malformed request
        500 — classification schema error
        502 — LLM invocation failed
    """
    try:
        raw_body = req.get_body().decode("utf-8")
    except Exception:
        raw_body = ""

    try:
        data: dict = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON body: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        email = EmailMessage(
            message_id   = data.get("messageId", "unknown"),
            from_address = data.get("fromAddress", "unknown@example.com"),
            from_name    = data.get("fromName") or None,
            to_address   = data.get("toAddress") or "",
            subject      = data.get("subject")   or "",
            body_text    = data.get("bodyText")   or "",
            received_at  = data.get("receivedAt") or "",
            source       = data.get("source")     or "webhook",
        )
    except Exception as exc:
        logger.error("Failed to build EmailMessage: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": f"Invalid email payload: {exc}"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        record = classify(email, _get_adapter())
    except LLMInvocationError as exc:
        logger.error("LLM invocation failed: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": "LLM invocation failed"}),
            status_code=502,
            mimetype="application/json",
        )
    except ValueError as exc:
        logger.error("Classification schema error: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": "Classification failed"}),
            status_code=500,
            mimetype="application/json",
        )

    stored = True
    try:
        _save_to_cosmos(record)
    except Exception as exc:
        stored = False
        logger.error("Cosmos DB write failed (record still logged): %s", exc)

    # Route + dispatch (non-fatal — Slack/HubSpot/etc. may not be configured)
    try:
        decision = route(record.result)
        dispatch(record, decision)
    except Exception as exc:
        logger.warning("Routing/dispatch failed (non-fatal): %s", exc)

    _otel_flush()

    if not stored:
        return func.HttpResponse(
            json.dumps({"status": "classified_not_stored", "record_id": record.record_id}),
            status_code=202,
            mimetype="application/json",
        )

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

    return func.HttpResponse(
        json.dumps({
            "status":         "ok",
            "record_id":      record.record_id,
            "intent":         record.result.intent.value,
            "urgency":        record.result.urgency.value,
            "confidence":     record.result.confidence,
            "requires_human": record.result.requires_human,
            "summary":        record.result.summary,
        }),
        status_code=200,
        mimetype="application/json",
    )
