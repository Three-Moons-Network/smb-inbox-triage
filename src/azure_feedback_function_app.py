# -*- coding: utf-8 -*-
"""
Azure Functions v2 entry point — feedback function.

Route: POST /api/feedback

Accepts a human correction with an HMAC-SHA256 signature, validates it,
and patches the existing Cosmos DB classification record.

Env vars
--------
  CLOUD                         = "azure"
  COSMOS_CONNECTION_STRING      Cosmos DB account endpoint URL
  COSMOS_DATABASE               e.g. inbox-triage
  COSMOS_CONTAINER_CLASSIFICATIONS  e.g. classifications
  FEEDBACK_WEBHOOK_SECRET       HMAC secret — resolved from Key Vault at runtime
                                (set FEEDBACK_AUTH_DISABLED=true in dev to bypass)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types

import azure.functions as func

# ── sys.modules alias ─────────────────────────────────────────────────────────

# Pass 1 — leaf modules
import classifier.models      # noqa: E402
import observability.tracing  # noqa: E402

# Pass 2 — src namespace
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

# Pass 3 — modules with src.* deps
import feedback.handler                                                      # noqa: E402
sys.modules.setdefault("src.feedback", sys.modules.get("feedback"))
sys.modules["src.feedback.handler"] = sys.modules["feedback.handler"]

import feedback.store                                                        # noqa: E402
sys.modules["src.feedback.store"] = sys.modules["feedback.store"]

# ── Application imports ────────────────────────────────────────────────────────
from feedback.handler import (          # noqa: E402
    handle_feedback_request,
    FeedbackAuthError,
    FeedbackValidationError,
)
from observability.tracing import flush as _otel_flush  # noqa: E402

logger = logging.getLogger(__name__)

# ── Azure Functions app ────────────────────────────────────────────────────────

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="feedback", methods=["POST"])
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/feedback — submit a human correction.

    Body:
        {"record_id": "<uuid>", "corrected_intent": "<intent>", "reviewer": "<name>"}

    Header (required unless FEEDBACK_AUTH_DISABLED=true):
        X-Webhook-Signature: sha256=<hmac_hex>

    Response codes:
        200 — correction stored
        400 — invalid JSON
        401 — missing or invalid signature
        422 — validation error (bad intent value, missing fields)
        404 — record_id not found in Cosmos DB
    """
    try:
        raw_body = req.get_body().decode("utf-8")
    except Exception:
        raw_body = ""

    sig = req.headers.get("X-Webhook-Signature")

    try:
        result = handle_feedback_request(raw_body, signature_header=sig)
    except FeedbackAuthError as exc:
        logger.warning("Feedback auth failure: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=401,
            mimetype="application/json",
        )
    except FeedbackValidationError as exc:
        logger.warning("Feedback validation failure: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=422,
            mimetype="application/json",
        )
    except KeyError as exc:
        logger.warning("Feedback record not found: %s", exc)
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=404,
            mimetype="application/json",
        )
    except json.JSONDecodeError as exc:
        _otel_flush()
        return func.HttpResponse(
            json.dumps({"error": f"Invalid JSON body: {exc}"}),
            status_code=400,
            mimetype="application/json",
        )

    _otel_flush()
    return func.HttpResponse(
        json.dumps(result),
        status_code=200,
        mimetype="application/json",
    )
