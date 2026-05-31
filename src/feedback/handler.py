# -*- coding: utf-8 -*-
"""
Feedback webhook handler — cloud-agnostic.

Accepts POST requests with:
  Header:  X-Webhook-Signature: sha256=<hex>
  Body:    {"record_id": "<uuid>", "corrected_intent": "<intent>", "reviewer": "<name>"}

Callers compute the signature as:
  HMAC-SHA256(FEEDBACK_WEBHOOK_SECRET, request_body_bytes).hexdigest()
  and send it as: X-Webhook-Signature: sha256=<hex>

Review fixes applied
--------------------
P1  — HMAC-SHA256 webhook authentication added.  Set FEEDBACK_WEBHOOK_SECRET
      to a random secret (min 32 bytes) in Secrets Manager / Key Vault.
      Unsigned requests are rejected with a 401-equivalent error.
      Auth can be disabled by setting FEEDBACK_AUTH_DISABLED=true (CI / tests only).
P15 — data.get() calls now guard against non-string values with explicit type check
      before calling .strip(); raises 422-equivalent FeedbackValidationError.
OTel — record_feedback() emits a Datadog span after successful write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from src.classifier.models import Intent
from src.feedback.store import write_feedback
from src.observability.tracing import record_feedback as _otel_record_feedback

logger = logging.getLogger(__name__)

VALID_INTENTS = {i.value for i in Intent}

_AUTH_DISABLED_ENVS = {"true", "1", "yes"}


class FeedbackValidationError(ValueError):
    """Raised on missing / invalid fields — caller should return HTTP 422."""


class FeedbackAuthError(PermissionError):
    """Raised on missing / invalid signature — caller should return HTTP 401."""


# ── HMAC authentication (P1) ─────────────────────────────────────────────────

def _auth_disabled() -> bool:
    return os.environ.get("FEEDBACK_AUTH_DISABLED", "false").lower() in _AUTH_DISABLED_ENVS


def verify_signature(body_bytes: bytes, signature_header: str | None) -> None:
    """
    Validate the HMAC-SHA256 signature on the request body.

    Args:
        body_bytes:       Raw request body bytes.
        signature_header: Value of the X-Webhook-Signature header,
                          expected format: "sha256=<hex_digest>"

    Raises:
        FeedbackAuthError: if authentication fails.
    """
    if _auth_disabled():
        return  # tests / CI bypass

    secret_str = os.environ.get("FEEDBACK_WEBHOOK_SECRET", "")
    if not secret_str:
        raise FeedbackAuthError(
            "FEEDBACK_WEBHOOK_SECRET is not configured. "
            "Set it in Secrets Manager / Key Vault before enabling the feedback endpoint."
        )

    if not signature_header:
        raise FeedbackAuthError("Missing X-Webhook-Signature header")

    # Parse "sha256=<hex>"
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        raise FeedbackAuthError(
            "Malformed X-Webhook-Signature header — expected 'sha256=<hex_digest>'"
        )

    expected = hmac.new(
        secret_str.encode(),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(expected, parts[1].lower()):
        raise FeedbackAuthError("Webhook signature verification failed")


# ── Request handler ───────────────────────────────────────────────────────────

def handle_feedback_request(
    body: str | bytes | dict,
    signature_header: str | None = None,
) -> dict[str, Any]:
    """
    Parse, authenticate, validate, and persist a feedback correction.

    Args:
        body:             Raw request body (JSON string, bytes, or already-parsed dict).
        signature_header: Value of X-Webhook-Signature header (required unless
                          FEEDBACK_AUTH_DISABLED=true).

    Returns:
        Dict suitable for JSON serialisation as an HTTP response body.

    Raises:
        FeedbackAuthError:       on missing or invalid signature.
        FeedbackValidationError: on missing or invalid fields.
    """
    # Normalise body to bytes for auth, dict for validation
    if isinstance(body, dict):
        body_bytes = json.dumps(body, separators=(",", ":")).encode()
        data = body
    elif isinstance(body, str):
        body_bytes = body.encode()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise FeedbackValidationError(f"Invalid JSON body: {exc}") from exc
    else:
        body_bytes = bytes(body)
        try:
            data = json.loads(body_bytes)
        except json.JSONDecodeError as exc:
            raise FeedbackValidationError(f"Invalid JSON body: {exc}") from exc

    # P1: authenticate before processing
    verify_signature(body_bytes, signature_header)

    # P15: explicit type guards before calling .strip()
    raw_record_id       = data.get("record_id")
    raw_corrected_intent = data.get("corrected_intent")
    raw_reviewer        = data.get("reviewer")

    if not isinstance(raw_record_id, str):
        raise FeedbackValidationError(
            f"Missing or invalid field 'record_id' — expected string, got {type(raw_record_id).__name__}"
        )
    if not isinstance(raw_corrected_intent, str):
        raise FeedbackValidationError(
            f"Missing or invalid field 'corrected_intent' — expected string, got {type(raw_corrected_intent).__name__}"
        )

    record_id        = raw_record_id.strip()
    corrected_intent = raw_corrected_intent.strip()
    reviewer = (raw_reviewer.strip() if isinstance(raw_reviewer, str) else "anonymous")

    if not record_id:
        raise FeedbackValidationError("Missing required field: record_id")
    if not corrected_intent:
        raise FeedbackValidationError("Missing required field: corrected_intent")
    if corrected_intent not in VALID_INTENTS:
        raise FeedbackValidationError(
            f"Invalid intent {corrected_intent!r}. Valid values: {sorted(VALID_INTENTS)}"
        )

    write_feedback(record_id=record_id, corrected_intent=corrected_intent, reviewer=reviewer)

    # OTel: emit feedback correction span
    _otel_record_feedback(record_id, corrected_intent, reviewer)

    return {
        "status":           "ok",
        "record_id":        record_id,
        "corrected_intent": corrected_intent,
        "reviewer":         reviewer,
    }
