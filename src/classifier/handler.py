# -*- coding: utf-8 -*-
"""
Cloud-agnostic classifier entry point.

Usage::

    from src.classifier.handler import classify
    from src.adapters.aws_bedrock import BedrockAdapter

    adapter = BedrockAdapter()
    record  = classify(email, adapter)

Review fixes applied
--------------------
P9  — datetime.now(timezone.utc) replaces utcnow() throughout.
P19 — raw LLM output is never logged; only a safe excerpt of the parse error.
P29 — classified_at uses the same tz-aware path as the rest of the codebase.
P39 — datetime imported at top of module, not inside the function.
P47 — LLMAdapter Protocol decorated with @runtime_checkable.
OTel — span wraps the entire classify() call; record_llm_call() emits the
       LLM-Observability span for Datadog.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from src.classifier.models import (
    ClassificationRecord,
    ClassificationResult,
    EmailMessage,
)
from src.classifier.prompts import SYSTEM_PROMPT, build_user_message
from src.observability.tracing import record_llm_call, span

logger = logging.getLogger(__name__)


# ── Adapter protocol ──────────────────────────────────────────────────────────

@runtime_checkable  # P47: enables isinstance(obj, LLMAdapter) checks
class LLMAdapter(Protocol):
    """
    Any object satisfying this protocol can be passed to classify().
    Each cloud adapter implements these two attributes and one method.
    """

    model_id: str  # e.g. "claude-3-haiku-20240307"
    cloud: str     # "aws" | "azure" | "gcp"

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int]:
        """
        Call the LLM and return (raw_json_str, input_tokens, output_tokens).
        Raises LLMInvocationError on hard failures.
        """
        ...


class LLMInvocationError(Exception):
    """Raised when the LLM call fails after retries."""


# ── Core classify function ─────────────────────────────────────────────────────

def classify(email: EmailMessage, adapter: LLMAdapter) -> ClassificationRecord:
    """
    Classify a single email using the provided LLM adapter.

    Returns a ClassificationRecord ready to be persisted to the datastore.

    Raises:
        LLMInvocationError: if the adapter fails to get a response.
        ValueError: if the LLM returns malformed JSON that doesn't match the schema.
    """
    record_id = str(uuid.uuid4())

    user_message = build_user_message(
        subject=email.subject,
        from_address=email.from_address,
        from_name=email.from_name,
        body_text=email.body_text,
    )

    logger.info(
        "Classifying email",
        extra={
            "record_id": record_id,
            "message_id": email.message_id,
            "model": adapter.model_id,
            "cloud": adapter.cloud,
        },
    )

    with span(
        "classifier.classify_email",
        cloud=adapter.cloud,
        model=adapter.model_id,
        message_id=email.message_id,
        record_id=record_id,
    ):
        t0 = time.monotonic()
        raw_json, input_tokens, output_tokens = adapter.invoke(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Parse and validate against the Pydantic schema.
        # P19: never log raw_json — it contains attacker-controlled content.
        try:
            parsed = json.loads(raw_json)
            result = ClassificationResult.model_validate(parsed)
        except json.JSONDecodeError as exc:
            logger.error(
                "LLM returned invalid JSON (record_id=%s): %s",
                record_id,
                str(exc)[:200],  # log the parse error, NOT the raw output
            )
            raise ValueError(f"LLM returned malformed JSON: {exc}") from exc
        except Exception as exc:
            logger.error(
                "LLM output failed schema validation (record_id=%s): %s",
                record_id,
                str(exc)[:200],
            )
            raise ValueError(f"LLM returned malformed JSON: {exc}") from exc

        # P9, P29: consistent tz-aware ISO-8601 timestamp
        classified_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        record = ClassificationRecord(
            record_id=record_id,
            email=email,
            result=result,
            model_id=adapter.model_id,
            cloud=adapter.cloud,
            latency_ms=latency_ms,
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            classified_at=classified_at,
        )

        # OTel: annotate the active classifier span with result attrs.
        # Called HERE (inside the with span() block) so the attributes land on
        # classifier.classify_email, not on a detached sibling span.
        record_llm_call(record)

    logger.info(
        "Classification complete",
        extra={
            "record_id": record_id,
            "intent": result.intent.value,
            "confidence": result.confidence,
            "latency_ms": latency_ms,
        },
    )

    return record
