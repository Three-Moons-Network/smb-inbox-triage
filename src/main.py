# -*- coding: utf-8 -*-
"""
Cloud Functions 2nd gen entry points for GCP deployment.

    handle_webhook  — Receives Pub/Sub push from Eventarc, classifies email
    handle_feedback — Receives human corrections via POST

Import-path note
----------------
Terraform's `archive_file` datasource zips the *contents* of `src/` directly
(source_dir = "...src"), so at runtime the zip root contains:

    classifier/   adapters/   feedback/   observability/   router/

The rest of the codebase uses `from src.xxx import` (natural for local dev
where `src/` is a sub-directory under the project root).  Cloud Functions
adds the zip root to sys.path, so there is no `src` package unless we
create one.  The block below injects a namespace-module pointing `src` at
the zip root, satisfying those imports without touching any existing file.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import types

# ── sys.modules alias: make 'from src.xxx import' work at the zip root ────────
#
# Problem (naive approach): creating a fake `src` namespace and letting Python
# import `src.classifier.models` on demand produces a SECOND module object for
# the same .py file whenever both of these appear in the import graph:
#
#     from classifier.models import EmailMessage      # module object A
#     from src.classifier.models import EmailMessage  # module object B  ← NEW
#
# Pydantic v2 validates field types by class identity (type(value) is FieldType),
# so passing an instance of class-A to a field annotated with class-B raises:
#   "Input should be a valid dictionary or instance of EmailMessage"
# even though A and B are byte-for-byte identical.
#
# Fix (two-pass import):
#   Pass 1 — import "leaf" modules (no src.* deps) via canonical path so they
#             get registered in sys.modules under their canonical key.
#   Pass 2 — create the src namespace and immediately register aliases that
#             point to those SAME module objects.  Subsequent `from src.xxx`
#             imports hit the cache and reuse the canonical objects.
#   Pass 3 — import modules that DO have src.* deps; they find the pre-seeded
#             aliases and never create duplicate class objects.

_here = os.path.dirname(os.path.abspath(__file__))

# Pass 1 — leaf modules (stdlib + third-party only, no src.* imports)
import classifier.models      # noqa: E402
import observability.tracing  # noqa: E402

# Pass 2 — create src namespace and alias already-loaded canonical modules
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
# Register each alias immediately so the next module in the chain finds it.
import classifier.handler                                                    # noqa: E402
sys.modules["src.classifier.handler"] = sys.modules["classifier.handler"]

import adapters.gcp_vertex                                                   # noqa: E402
sys.modules.setdefault("src.adapters", sys.modules.get("adapters"))
sys.modules["src.adapters.gcp_vertex"] = sys.modules["adapters.gcp_vertex"]

import feedback.handler                                                      # noqa: E402
sys.modules.setdefault("src.feedback", sys.modules.get("feedback"))
sys.modules["src.feedback.handler"] = sys.modules["feedback.handler"]

# ── Application imports (all module objects now canonical) ────────────────────
from classifier.handler import classify, LLMInvocationError  # noqa: E402
from adapters.gcp_vertex import VertexAIAdapter               # noqa: E402
from classifier.models import EmailMessage                     # noqa: E402
from feedback.handler import (                                 # noqa: E402
    handle_feedback_request,
    FeedbackAuthError,
    FeedbackValidationError,
)
from observability.tracing import flush as _otel_flush        # noqa: E402

logger = logging.getLogger(__name__)

# Singleton adapter — reused across warm invocations
_adapter = VertexAIAdapter()


# ── Firestore helper ──────────────────────────────────────────────────────────

def _save_to_firestore(record) -> None:
    """Persist a ClassificationRecord to Firestore classifications collection."""
    from google.cloud import firestore  # noqa: PLC0415 — lazy; only used on GCP

    project  = os.environ.get("GOOGLE_CLOUD_PROJECT")
    database = os.environ.get("FIRESTORE_DATABASE", "(default)")

    db = firestore.Client(project=project, database=database)

    # Convert Pydantic model to a plain dict via JSON round-trip so that
    # enum values become strings and nested models become dicts.
    doc = json.loads(record.model_dump_json())

    db.collection("classifications").document(record.record_id).set(doc)
    logger.debug("Firestore write complete — record_id=%s", record.record_id)


# ── Webhook handler ───────────────────────────────────────────────────────────

def handle_webhook(request):
    """
    Cloud Functions 2nd gen HTTP handler — classifier.

    Accepts Pub/Sub push envelopes delivered by Eventarc from the Gmail Watch
    topic.  Also accepts direct test POSTs where the base64 `data` field
    contains a full EmailMessage JSON (used by the smoke test in GCP-DEPLOY.md).

    Response codes:
        200 — classified and stored successfully
        202 — classified but Firestore write failed (classification not lost in logs)
        400 — malformed request
        405 — wrong HTTP method
        500 — classification schema error
        502 — LLM invocation failed
    """
    if request.method != "POST":
        return ({"error": "Method Not Allowed"}, 405)

    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        return ({"error": "Invalid JSON body"}, 400)

    # ── Unwrap Pub/Sub push envelope ──────────────────────────────────────────
    message  = body.get("message", {})
    raw_data = message.get("data", "")
    if not raw_data:
        logger.error("Missing message.data in Pub/Sub envelope")
        _otel_flush()
        return ({"error": "Missing message.data"}, 400)

    try:
        decoded = base64.b64decode(raw_data).decode("utf-8")
        data    = json.loads(decoded)
    except Exception as exc:
        logger.error("Failed to decode message.data: %s", exc)
        _otel_flush()
        return ({"error": "Invalid base64/JSON in message.data"}, 400)

    # ── Real Gmail Watch push: historyId only, no email content ──────────────
    # Gmail Watch does NOT push email content — only a historyId notification.
    # A future iteration will call the Gmail API to fetch the message.
    # For now we ack immediately so Pub/Sub does not retry.
    if "historyId" in data and "messageId" not in data:
        logger.info(
            "Gmail Watch notification received (historyId=%s) — "
            "full Gmail API fetch not yet implemented; acking.",
            data.get("historyId"),
        )
        _otel_flush()
        return ({"status": "acked", "reason": "historyId_not_yet_fetched"}, 200)

    # ── Build EmailMessage from test / direct payload ─────────────────────────
    try:
        email = EmailMessage(
            message_id   = data.get("messageId", "unknown"),
            from_address = data.get("fromAddress", "unknown@example.com"),
            from_name    = data.get("fromName") or None,
            to_address   = data.get("toAddress") or "",
            subject      = data.get("subject")   or "",
            body_text    = data.get("bodyText")   or "",
            received_at  = data.get("receivedAt") or "",
            source       = data.get("source")     or "gcp_pubsub",
        )
    except Exception as exc:
        logger.error("Failed to build EmailMessage: %s", exc)
        _otel_flush()
        return ({"error": f"Invalid email payload: {exc}"}, 400)

    # ── Classify ──────────────────────────────────────────────────────────────
    try:
        record = classify(email, _adapter)
    except LLMInvocationError as exc:
        logger.error("LLM invocation failed: %s", exc)
        _otel_flush()
        return ({"error": "LLM invocation failed"}, 502)
    except ValueError as exc:
        logger.error("Classification schema error: %s", exc)
        _otel_flush()
        return ({"error": "Classification failed"}, 500)

    # ── Persist ───────────────────────────────────────────────────────────────
    try:
        _save_to_firestore(record)
    except Exception as exc:
        # Storage failure is recoverable — logs have the full record.
        # Return 202 so the caller knows classification succeeded.
        logger.error("Firestore write failed (record still logged): %s", exc)
        _otel_flush()
        return ({"status": "classified_not_stored", "record_id": record.record_id}, 202)

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
    _otel_flush()
    return ({"status": "ok", "record_id": record.record_id}, 200)


# ── Feedback handler ──────────────────────────────────────────────────────────

def handle_feedback(request):
    """
    Cloud Functions 2nd gen HTTP handler — human correction webhook.

    Expects POST with:
        Header: X-Webhook-Signature: sha256=<hex>
        Body:   {"record_id": "...", "corrected_intent": "...", "reviewer": "..."}

    Response codes:
        200 — correction accepted
        401 — missing or invalid signature
        405 — wrong HTTP method
        422 — missing or invalid fields
        500 — internal error
    """
    # Cloud Run V2 throttles CPU between requests, so the BatchSpanProcessor
    # background thread can't drain on its own. Wrap every return path in
    # try/finally to guarantee force_flush() runs before the container freezes.
    try:
        if request.method != "POST":
            return ({"error": "Method Not Allowed"}, 405)

        signature = request.headers.get("X-Webhook-Signature")
        body      = request.get_data()

        try:
            result = handle_feedback_request(body=body, signature_header=signature)
            return (result, 200)
        except FeedbackAuthError as exc:
            logger.warning("Feedback auth failure: %s", exc)
            return ({"error": "Unauthorized"}, 401)
        except FeedbackValidationError as exc:
            logger.warning("Feedback validation failure: %s", exc)
            return ({"error": str(exc)}, 422)
        except KeyError as exc:
            logger.warning("Feedback record not found: %s", exc)
            return ({"error": f"Record not found: {exc}"}, 404)
        except Exception as exc:
            logger.error("Feedback handler error: %s", exc)
            return ({"error": "Internal server error"}, 500)
    finally:
        _otel_flush()
