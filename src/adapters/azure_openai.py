# -*- coding: utf-8 -*-
"""
Azure OpenAI adapter — Chat Completions with strict json_schema response_format.

Env vars required
-----------------
  AZURE_OPENAI_ENDPOINT     e.g. https://tmn-inbox.openai.azure.com/
  AZURE_OPENAI_API_KEY      fetched from Key Vault at cold start via KV reference
  AZURE_OPENAI_DEPLOYMENT   e.g. gpt-4o-mini
  AZURE_OPENAI_API_VERSION  e.g. 2024-08-01-preview

Review fixes applied
--------------------
P6  — Hand-rolled JSON Schema (no $ref / $defs / anyOf) for OpenAI strict mode.
      Pydantic's model_json_schema() emits $ref/$defs that cause HTTP 400.
      All enum values are inlined; nullable fields use ["string","null"] type array.
P17 — RateLimitError retried with exponential back-off (up to 3 attempts).
P26 — temperature=0 for deterministic classification.
P27 — httpx timeout passed to AzureOpenAI client (30 s).
P41 — Missing AZURE_OPENAI_ENDPOINT raises a descriptive ValueError at cold start.
N10 — Jitter (±25%) added to retry wait — prevents thundering-herd under rate storms.
"""

from __future__ import annotations

import logging
import os
import random
import time

from openai import AzureOpenAI, APIError, RateLimitError

from src.classifier.handler import LLMInvocationError
from src.observability.tracing import span as _span

logger = logging.getLogger(__name__)

# ── Hand-rolled strict JSON schema (P6) ──────────────────────────────────────
#
# OpenAI strict mode requirements:
#   • No $ref, $defs, or anyOf at the schema root or in property definitions
#   • additionalProperties: false on every object
#   • All properties listed in "required"
#   • Nullable string: type array ["string", "null"]
#
_STRICT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent", "urgency", "sentiment", "summary",
        "order_id", "sender_name",
        "confidence", "requires_human", "reasoning",
    ],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "sales_inquiry", "support_request", "billing_question",
                "vendor_outreach", "job_application", "marketing_noise",
                "urgent_escalation", "unknown",
            ],
        },
        "urgency": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative"],
        },
        "summary":        {"type": "string"},
        "order_id":       {"type": ["string", "null"]},   # nullable
        "sender_name":    {"type": ["string", "null"]},   # nullable
        "confidence":     {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "requires_human": {"type": "boolean"},
        "reasoning":      {"type": "string"},
    },
}

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name":   "ClassificationResult",
        "strict": True,
        "schema": _STRICT_SCHEMA,
    },
}

_MAX_RETRIES      = 3
_RETRY_BASE_DELAY = 2.0  # seconds


class AzureOpenAIAdapter:
    """LLMAdapter implementation backed by Azure OpenAI Chat Completions."""

    def __init__(
        self,
        deployment:  str | None = None,
        endpoint:    str | None = None,
        api_key:     str | None = None,
        api_version: str | None = None,
    ) -> None:
        self.model_id = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        self.cloud    = "azure"

        # P41: descriptive error for missing required env var
        resolved_endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not resolved_endpoint:
            raise ValueError(
                "Azure OpenAI endpoint not configured. "
                "Set AZURE_OPENAI_ENDPOINT environment variable to "
                "https://<your-resource>.openai.azure.com/"
            )

        self._client = AzureOpenAI(
            azure_endpoint=resolved_endpoint,
            api_key=api_key or os.environ.get("AZURE_OPENAI_API_KEY"),
            api_version=api_version or os.environ.get(
                "AZURE_OPENAI_API_VERSION", "2024-08-01-preview"
            ),
            timeout=30.0,  # P27: per-call timeout
        )

    def invoke(
        self,
        system_prompt: str,
        user_message:  str,
    ) -> tuple[str, int, int]:
        """
        Call Azure OpenAI with strict json_schema structured output.
        Returns (json_str, input_tokens, output_tokens).

        P17: retries RateLimitError with exponential back-off.
        """
        last_exc: Exception | None = None

        with _span(
            "gen_ai.azure_openai.chat",
            **{
                "gen_ai.system":              "azure.openai",
                "gen_ai.request.model":       self.model_id,
                "gen_ai.request.temperature": 0,
            },
        ) as s:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = self._client.chat.completions.create(
                        model=self.model_id,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_message},
                        ],
                        response_format=_RESPONSE_FORMAT,
                        temperature=0,   # P26: deterministic
                    )
                    break  # success

                except RateLimitError as exc:
                    # N10: jitter prevents synchronized retries across concurrent instances
                    wait = _RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.75, 1.25)
                    logger.warning(
                        "Azure OpenAI RateLimitError on attempt %d/%d — retrying in %.1fs",
                        attempt + 1, _MAX_RETRIES, wait,
                        extra={"attempt": attempt + 1},
                    )
                    last_exc = exc
                    time.sleep(wait)
                    continue

                except APIError as exc:
                    logger.error(
                        "Azure OpenAI APIError: %s", type(exc).__name__,
                        extra={"error_type": type(exc).__name__},
                    )
                    raise LLMInvocationError(
                        f"Azure OpenAI call failed: {type(exc).__name__}"
                    ) from exc
            else:
                raise LLMInvocationError(
                    f"Azure OpenAI call failed after {_MAX_RETRIES} retries (rate limit): {last_exc}"
                ) from last_exc

            choice = response.choices[0]

            if choice.finish_reason == "content_filter":
                raise LLMInvocationError("Azure OpenAI content filter triggered")

            raw_json      = choice.message.content or ""
            usage         = response.usage
            input_tokens  = usage.prompt_tokens     if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            finish_reason = choice.finish_reason or "stop"

            # Datadog LLM Observability reads gen_ai.* span attributes
            s.set_attribute("gen_ai.usage.input_tokens",      input_tokens)
            s.set_attribute("gen_ai.usage.output_tokens",     output_tokens)
            s.set_attribute("gen_ai.response.finish_reasons", finish_reason)

            logger.info(
                "Azure OpenAI chat completed",
                extra={
                    "model_id":      self.model_id,
                    "input_tokens":  input_tokens,
                    "output_tokens": output_tokens,
                    "finish_reason": finish_reason,
                },
            )

            return raw_json, input_tokens, output_tokens
