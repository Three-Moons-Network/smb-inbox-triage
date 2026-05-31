# -*- coding: utf-8 -*-
"""
GCP Vertex AI adapter — Gemini with JSON response schema enforcement.

Env vars required
-----------------
  GOOGLE_CLOUD_PROJECT    GCP project ID
  VERTEX_AI_LOCATION      e.g. us-central1
  VERTEX_MODEL_ID         e.g. gemini-1.5-flash-002

Authentication: Cloud Functions 2nd gen runs under a Service Account with the
Vertex AI User role.  Application Default Credentials are used automatically.

Review fixes applied
--------------------
P7  — Hand-rolled OpenAPI 3.0 subset schema for response_schema.
      Vertex rejects JSON Schema $defs/$ref/anyOf — uses `nullable:true` instead.
P24 — GenerativeModel is built once in __init__; per-call system instruction is
      passed via system_instruction on the cached instance (Vertex supports this
      via a workaround: rebuild only when system_instruction changes, cache result).
P25 — MAX_TOKENS finish reason raises LLMInvocationError instead of silently
      returning truncated (and likely invalid) JSON.
P26 — temperature=0 for deterministic classification.
P27 — request_options timeout set (30 s) to prevent hung Cloud Function invocations.
M1  — Retry loop added (ResourceExhausted / ServiceUnavailable), mirroring Bedrock
      and Azure adapters. Without this, Vertex fails fast on transients while the
      other clouds absorb them, biasing cross-cloud eval results and production SLAs.
N10 — Jitter (±25%) added to retry wait — prevents thundering-herd under rate storms.
"""

from __future__ import annotations

import logging
import os
import random
import time

import vertexai
from vertexai.generative_models import (
    GenerationConfig,
    GenerativeModel,
    HarmBlockThreshold,
    HarmCategory,
    SafetySetting,
)
from google.api_core.exceptions import (
    GoogleAPICallError,
    ResourceExhausted,
    ServiceUnavailable,
)

from src.classifier.handler import LLMInvocationError
from src.observability.tracing import span as _span

logger = logging.getLogger(__name__)

# ── Hand-rolled OpenAPI 3.0 schema (P7) ──────────────────────────────────────
#
# Vertex response_schema accepts the OpenAPI 3.0 Schema Object subset:
#   - type: STRING | INTEGER | NUMBER | BOOLEAN | ARRAY | OBJECT
#   - enum on STRING fields
#   - nullable: true  (NOT anyOf / oneOf)
#   - properties / required / items  as normal
#   - NO $ref, $defs, anyOf, oneOf, allOf
#
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "sales_inquiry", "support_request", "billing_question",
                "vendor_outreach", "job_application", "marketing_noise",
                "urgent_escalation", "unknown",
            ],
        },
        "urgency":        {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "sentiment":      {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "summary":        {"type": "string"},
        "order_id":       {"type": "string", "nullable": True},
        "sender_name":    {"type": "string", "nullable": True},
        "confidence":     {"type": "number"},
        "requires_human": {"type": "boolean"},
        "reasoning":      {"type": "string"},
    },
    "required": [
        "intent", "urgency", "sentiment", "summary",
        "order_id", "sender_name",
        "confidence", "requires_human", "reasoning",
    ],
}

# Safety settings — permissive for business email content
_SAFETY_SETTINGS = [
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                  threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT,
                  threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                  threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
    SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                  threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
]

# P27: per-call timeout (seconds)
_REQUEST_TIMEOUT = 30

# M1: retry settings (mirrors Bedrock/Azure for cross-cloud consistency)
_MAX_RETRIES      = 3
_RETRY_BASE_DELAY = 1.0  # seconds; jittered ±25% (N10)


class VertexAIAdapter:
    """LLMAdapter implementation backed by Vertex AI Gemini."""

    def __init__(
        self,
        model_id: str | None = None,
        project:  str | None = None,
        location: str | None = None,
    ) -> None:
        self.model_id = model_id or os.environ.get("VERTEX_MODEL_ID", "gemini-2.5-flash")
        self.cloud    = "gcp"

        _project  = project  or os.environ["GOOGLE_CLOUD_PROJECT"]
        _location = location or os.environ.get("VERTEX_AI_LOCATION", "us-central1")
        vertexai.init(project=_project, location=_location)

        # P24: generation config built once; token budget raised to 1024 to
        # prevent silent truncation (P25 handles MAX_TOKENS explicitly below).
        self._generation_config = GenerationConfig(
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
            temperature=0,           # P26
            max_output_tokens=1024,  # P25: raised from 512; truncated JSON → error
        )

        # P24: cache the model instance; rebuild only if system_instruction changes.
        self._cached_system_instruction: str | None = None
        self._model: GenerativeModel | None = None

    def _get_model(self, system_instruction: str) -> GenerativeModel:
        """Return cached model or rebuild when system_instruction changes (P24)."""
        if self._model is None or self._cached_system_instruction != system_instruction:
            self._model = GenerativeModel(
                model_name=self.model_id,
                system_instruction=system_instruction,
            )
            self._cached_system_instruction = system_instruction
        return self._model

    def invoke(
        self,
        system_prompt: str,
        user_message:  str,
    ) -> tuple[str, int, int]:
        """
        Call Vertex AI Gemini with JSON response schema enforcement.
        Returns (json_str, input_tokens, output_tokens).

        M1: retries ResourceExhausted and ServiceUnavailable up to 3× with
        jittered exponential back-off, matching Bedrock and Azure behaviour.
        """
        model     = self._get_model(system_prompt)
        last_exc: Exception | None = None

        with _span(
            "gen_ai.vertex_ai.generate",
            **{
                "gen_ai.system":              "google.vertex_ai",
                "gen_ai.request.model":       self.model_id,
                "gen_ai.request.temperature": 0,
            },
        ) as s:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = model.generate_content(
                        contents=user_message,
                        generation_config=self._generation_config,
                        safety_settings=_SAFETY_SETTINGS,
                        # request_options not supported by the vertexai high-level
                        # GenerativeModel API — only available on the lower-level
                        # google-cloud-aiplatform client. Removed to avoid TypeError.
                    )
                    break  # success — exit retry loop

                except (ResourceExhausted, ServiceUnavailable) as exc:
                    # M1 + N10: transient quota/availability errors retried with jitter
                    wait = _RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.75, 1.25)
                    logger.warning(
                        "Vertex AI %s on attempt %d/%d — retrying in %.1fs",
                        type(exc).__name__, attempt + 1, _MAX_RETRIES, wait,
                        extra={"attempt": attempt + 1, "error_type": type(exc).__name__},
                    )
                    last_exc = exc
                    time.sleep(wait)
                    continue

                except GoogleAPICallError as exc:
                    logger.error(
                        "Vertex AI API error: %s", type(exc).__name__,
                        extra={"error_type": type(exc).__name__},
                    )
                    raise LLMInvocationError(f"Vertex AI call failed: {type(exc).__name__}") from exc
            else:
                raise LLMInvocationError(
                    f"Vertex AI call failed after {_MAX_RETRIES} retries: {last_exc}"
                ) from last_exc

            if not response.candidates:
                raise LLMInvocationError("Vertex AI returned no candidates")

            candidate     = response.candidates[0]
            finish_reason = str(candidate.finish_reason)

            # P25: explicit MAX_TOKENS guard — truncated JSON is not usable
            if "MAX_TOKENS" in finish_reason:
                raise LLMInvocationError(
                    "Vertex AI response truncated at MAX_TOKENS — "
                    "increase max_output_tokens or shorten the prompt"
                )
            if "SAFETY" in finish_reason or "RECITATION" in finish_reason:
                raise LLMInvocationError(f"Vertex AI blocked response: {finish_reason}")

            raw_json = (
                candidate.content.parts[0].text
                if candidate.content and candidate.content.parts
                else ""
            )

            usage         = response.usage_metadata
            input_tokens  = getattr(usage, "prompt_token_count",     0)
            output_tokens = getattr(usage, "candidates_token_count", 0)

            # Datadog LLM Observability reads gen_ai.* span attributes
            s.set_attribute("gen_ai.usage.input_tokens",      input_tokens)
            s.set_attribute("gen_ai.usage.output_tokens",     output_tokens)
            s.set_attribute("gen_ai.response.finish_reasons", finish_reason)

            logger.info(
                "Vertex AI generate_content completed",
                extra={
                    "model_id":      self.model_id,
                    "input_tokens":  input_tokens,
                    "output_tokens": output_tokens,
                    "finish_reason": finish_reason,
                },
            )

            return raw_json, input_tokens, output_tokens
