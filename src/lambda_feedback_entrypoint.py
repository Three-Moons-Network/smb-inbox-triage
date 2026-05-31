# -*- coding: utf-8 -*-
"""
AWS Lambda entry point — feedback function.

Handles API Gateway v2 (HTTP API) proxy events.

    POST /feedback  — accepts a human correction, validates the HMAC-SHA256
                      signature, and updates the DynamoDB feedback record.

Import-path note
----------------
Same sys.modules alias technique as lambda_entrypoint.py and src/main.py.
See lambda_entrypoint.py for full rationale.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types

# ── sys.modules alias: make 'from src.xxx import' work at the zip root ────────
# Pass 1 — leaf modules (no src.* imports)
import classifier.models      # noqa: E402
import observability.tracing  # noqa: E402

# Pass 2 — create src namespace
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
    API Gateway v2 HTTP proxy Lambda handler — feedback.

    Accepts a JSON body with a human correction and validates the HMAC-SHA256
    signature from the X-Webhook-Signature header.

    Expected request body::

        {
            "record_id":        "<uuid>",
            "corrected_intent": "<intent_value>",
            "reviewer":         "<name>"
        }

    Expected header::

        X-Webhook-Signature: sha256=<hex_digest>

    where the digest is HMAC-SHA256(FEEDBACK_WEBHOOK_SECRET, raw_body_bytes).

    Response codes:
        200 — correction accepted
        400 — malformed request body
        401 — missing or invalid signature
        404 — record_id not found in DynamoDB
        405 — wrong HTTP method
        422 — missing or invalid fields
        500 — internal error
    """
    method = (
        event.get("requestContext", {})
             .get("http", {})
             .get("method", "")
             .upper()
    )
    if method and method != "POST":
        return _response(405, {"error": "Method Not Allowed"})

    # Decode body
    raw_body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not raw_body:
        return _response(400, {"error": "Empty request body"})

    # Signature from API Gateway v2 headers (lowercase keys)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-webhook-signature")

    try:
        result = handle_feedback_request(
            body=raw_body.encode("utf-8"),
            signature_header=signature,
        )
        _otel_flush()
        return _response(200, result)

    except FeedbackAuthError as exc:
        logger.warning("Feedback auth failure: %s", exc)
        _otel_flush()
        return _response(401, {"error": "Unauthorized"})

    except FeedbackValidationError as exc:
        logger.warning("Feedback validation failure: %s", exc)
        _otel_flush()
        return _response(422, {"error": str(exc)})

    except KeyError as exc:
        # Raised by _write_dynamodb when record_id not found (P4)
        logger.warning("Feedback record not found: %s", exc)
        _otel_flush()
        return _response(404, {"error": f"Record not found: {exc}"})

    except Exception as exc:
        logger.error("Feedback handler error: %s", exc)
        _otel_flush()
        return _response(500, {"error": "Internal server error"})
