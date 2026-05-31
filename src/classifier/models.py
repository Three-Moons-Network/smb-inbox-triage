# -*- coding: utf-8 -*-
"""
Pydantic models shared across all cloud adapters.

The ClassificationResult JSON schema is generated from these models at import
time and embedded into each LLM prompt — keep prompt and model in sync automatically.

Review fixes applied
--------------------
P8  — explicit validator ordering note: force_human_on_unknown runs *before* the
       field is accepted, which requires Pydantic v2 `mode="before"` and that both
       `intent` and `confidence` are declared before `requires_human`.  A unit test
       (test_validator_field_order) guards this invariant.
P9  — all datetime stamps use datetime.now(timezone.utc) not utcnow().
P21 — body truncation emits a WARNING log so the operator sees lossy emails.
P30 — input_tokens / output_tokens typed as int with ge=0; None means "not reported".
P43 — `source` is now a StrEnum (EmailSource) rather than a free-form string.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_BODY_TRUNCATE_CHARS = 4000


# ── Enums ─────────────────────────────────────────────────────────────────────

class Intent(str, Enum):
    SALES_INQUIRY     = "sales_inquiry"
    SUPPORT_REQUEST   = "support_request"
    BILLING_QUESTION  = "billing_question"
    VENDOR_OUTREACH   = "vendor_outreach"
    JOB_APPLICATION   = "job_application"
    MARKETING_NOISE   = "marketing_noise"
    URGENT_ESCALATION = "urgent_escalation"
    UNKNOWN           = "unknown"


class Urgency(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL  = "neutral"
    NEGATIVE = "negative"


class EmailSource(str, Enum):
    """Known inbound email sources — P43."""
    GMAIL    = "gmail"
    GRAPH_API = "graph_api"
    TEST     = "test"        # used by the eval harness and unit tests
    WEBHOOK  = "webhook"     # generic HTTP push


# ── Input model ───────────────────────────────────────────────────────────────

class EmailMessage(BaseModel):
    """Normalised inbound email — same shape regardless of source."""

    message_id:  str           = Field(description="Unique message identifier from the mail provider")
    thread_id:   Optional[str] = Field(default=None, description="Thread/conversation ID")
    from_address: str          = Field(description="Sender email address")
    from_name:   Optional[str] = Field(default=None, description="Sender display name if available")
    to_address:  str           = Field(description="Primary recipient address")
    subject:     str           = Field(description="Email subject line")
    body_text:   str           = Field(description="Plain-text body (stripped of HTML)")
    body_html:   Optional[str] = Field(default=None, description="Original HTML body if available")
    received_at: str           = Field(description="ISO-8601 timestamp of receipt")
    source:      str           = Field(description="Email provider — see EmailSource enum")

    @field_validator("body_text")
    @classmethod
    def truncate_body(cls, v: str) -> str:
        """Keep classifier prompt within token budget.

        P21: emit a WARNING when truncation occurs so operators can see
        that emails are being processed with incomplete bodies.
        """
        if len(v) > _BODY_TRUNCATE_CHARS:
            logger.warning(
                "Email body truncated from %d to %d chars — classification may miss context",
                len(v),
                _BODY_TRUNCATE_CHARS,
            )
            return v[:_BODY_TRUNCATE_CHARS]
        return v


# ── LLM output model ──────────────────────────────────────────────────────────
#
# IMPORTANT — field declaration order matters for P8.
# `force_human_on_unknown` is a mode="before" validator on `requires_human`.
# Pydantic v2 populates `info.data` with fields that have already been validated.
# `intent` and `confidence` MUST be declared before `requires_human` so they are
# available in `info.data` when the validator runs.

class ClassificationResult(BaseModel):
    """
    Structured output returned by every LLM adapter.
    The JSON schema generated from this model is embedded in the classifier prompt.
    """

    # ── Fields validated BEFORE requires_human — order is load-bearing (P8) ──
    intent:    Intent    = Field(description="Primary intent of this email")
    urgency:   Urgency   = Field(description="How urgently this needs a response")
    sentiment: Sentiment = Field(description="Overall emotional tone of the email")
    summary:   str       = Field(description="One-sentence summary suitable for a Slack notification")
    order_id:  Optional[str] = Field(
        default=None,
        description="Order or ticket number extracted from the email body, if present",
    )
    sender_name: Optional[str] = Field(
        default=None,
        description="Sender's name as they identify themselves in the email body",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Classifier confidence in the intent label (0.0–1.0)",
    )

    # ── requires_human — validated AFTER intent and confidence ────────────────
    requires_human: bool = Field(
        description=(
            "True when confidence < 0.75 or intent is UNKNOWN. "
            "Set automatically by force_human_on_unknown validator."
        )
    )
    reasoning: str = Field(
        description="One-sentence explanation of why this intent was chosen (for feedback loop)"
    )

    @field_validator("requires_human", mode="before")
    @classmethod
    def force_human_on_unknown(cls, v: bool, info) -> bool:  # type: ignore[override]
        """
        Always require human review for UNKNOWN intent or low confidence.

        P8: this validator relies on `intent` and `confidence` being present in
        `info.data`.  Both fields are declared before `requires_human` in the
        class body, which guarantees Pydantic v2 has validated them first.
        If this validator ever starts allowing UNKNOWN / low-confidence emails
        through, check that the field order hasn't been changed.
        """
        data = info.data
        intent = data.get("intent")
        confidence = data.get("confidence", 1.0)

        if intent == Intent.UNKNOWN:
            return True
        if isinstance(confidence, float) and confidence < 0.75:
            return True
        return bool(v)


# ── Persisted record ──────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix — P9."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ClassificationRecord(BaseModel):
    """
    Persisted to the datastore after every classification.
    Includes both the input email and the LLM output for audit + feedback loop.
    """

    record_id:   str           = Field(description="UUID generated at classification time")
    email:       EmailMessage
    result:      ClassificationResult
    model_id:    str           = Field(description="LLM model identifier")
    cloud:       str           = Field(description="Cloud platform: aws | azure | gcp")
    latency_ms:  int           = Field(description="End-to-end LLM call latency in milliseconds")
    # P30: typed as non-negative int; None means the adapter did not report usage
    input_tokens:  Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    classified_at: str           = Field(
        default_factory=_utcnow_iso,
        description="ISO-8601 UTC timestamp",
    )
    feedback_correction: Optional[str] = Field(
        default=None,
        description="Human-corrected intent, populated via feedback webhook",
    )
