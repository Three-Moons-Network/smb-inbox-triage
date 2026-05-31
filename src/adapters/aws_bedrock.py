# -*- coding: utf-8 -*-
"""
AWS Bedrock adapter — Converse API with tool_use for structured JSON output.

Env vars required
-----------------
  AWS_REGION           e.g. us-east-1
  BEDROCK_MODEL_ID     e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0
                       (the "us." prefix is the cross-region inference profile)

IAM permission required on the Lambda execution role:
  bedrock:InvokeModel  on arn:aws:bedrock:<region>::foundation-model/<model_id>

Review fixes applied
--------------------
P16 — ThrottlingException retried with exponential back-off (up to 3 attempts).
P26 — temperature=0 via additionalModelRequestFields for deterministic output.
P27 — botocore connect/read timeouts set (10 s) to prevent hung Lambda invocations.
Review fixes applied
--------------------
P16 — ThrottlingException retried with exponential back-off (up to 3 attempts).
P26 — temperature=0 via additionalModelRequestFields for deterministic output.
P27 — botocore connect/read timeouts set (10 s) to prevent hung Lambda invocations.
N7  — Removed __import__() hack; Intent imported directly from classifier.models.
N10 — Jitter (±25%) added to retry wait — prevents thundering-herd under rate storms.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.classifier.handler import LLMInvocationError
from src.classifier.models import ClassificationResult, Intent
from src.observability.tracing import span as _span

logger = logging.getLogger(__name__)

# Flat, inlined JSON schema — no $ref/$defs (safer for Bedrock tool schemas)
_CLASSIFY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent":         {"type": "string", "enum": [i.value for i in Intent]},
        "urgency":        {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "sentiment":      {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "summary":        {"type": "string"},
        "order_id":       {"type": "string"},
        "sender_name":    {"type": "string"},
        "confidence":     {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "requires_human": {"type": "boolean"},
        "reasoning":      {"type": "string"},
    },
    "required": ["intent", "urgency", "sentiment", "summary",
                 "sender_name", "confidence", "requires_human", "reasoning"],
    "additionalProperties": False,
}

_CLASSIFY_TOOL: dict[str, Any] = {
    "toolSpec": {
        "name":        "classify_email",
        "description": "Classify an inbound business email and extract structured fields.",
        "inputSchema": {"json": _CLASSIFY_TOOL_SCHEMA},
    }
}

# P27: explicit connect + read timeouts
_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"max_attempts": 0},  # we handle retries ourselves (P16)
)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds


class BedrockAdapter:
    """LLMAdapter implementation backed by Amazon Bedrock Converse API."""

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
    ) -> None:
        self.model_id = model_id or os.environ.get(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        self.cloud = "aws"
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
            config=_BOTO_CONFIG,
        )

    def invoke(
        self,
        system_prompt: str,
        user_message: str,
    ) -> tuple[str, int, int]:
        """
        Call Bedrock Converse API with tool_use.
        Returns (json_str, input_tokens, output_tokens).

        P16: retries ThrottlingException with exponential back-off.
        """
        last_exc: Exception | None = None

        with _span(
            "gen_ai.bedrock.converse",
            **{
                "gen_ai.system":             "aws.bedrock",
                "gen_ai.request.model":      self.model_id,
                "gen_ai.request.temperature": 0,
            },
        ) as s:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = self._client.converse(
                        modelId=self.model_id,
                        system=[{"text": system_prompt}],
                        messages=[{"role": "user", "content": [{"text": user_message}]}],
                        toolConfig={
                            "tools": [_CLASSIFY_TOOL],
                            "toolChoice": {"tool": {"name": "classify_email"}},
                        },
                        # P26: temperature via additionalModelRequestFields (Converse API)
                        additionalModelRequestFields={"temperature": 0},
                    )
                    break  # success

                except ClientError as exc:
                    error_code = exc.response.get("Error", {}).get("Code", "")
                    if error_code in ("ThrottlingException", "ServiceUnavailableException"):
                        # N10: jitter prevents thundering-herd when concurrent Lambdas
                        # all hit the same throttle and would otherwise retry in lockstep.
                        wait = _RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.75, 1.25)
                        logger.warning(
                            "Bedrock %s on attempt %d/%d — retrying in %.1fs",
                            error_code, attempt + 1, _MAX_RETRIES, wait,
                            extra={"attempt": attempt + 1, "error_code": error_code},
                        )
                        last_exc = exc
                        time.sleep(wait)
                        continue
                    logger.error(
                        "Bedrock Converse ClientError: %s", error_code,
                        extra={"error_code": error_code},
                    )
                    raise LLMInvocationError(f"Bedrock call failed: {error_code}") from exc

                except BotoCoreError as exc:
                    logger.error(
                        "Bedrock BotoCoreError: %s", type(exc).__name__,
                        extra={"error_type": type(exc).__name__},
                    )
                    raise LLMInvocationError(f"Bedrock call failed: {exc}") from exc
            else:
                raise LLMInvocationError(
                    f"Bedrock call failed after {_MAX_RETRIES} retries: {last_exc}"
                ) from last_exc

            usage         = response.get("usage", {})
            input_tokens  = usage.get("inputTokens",  0)
            output_tokens = usage.get("outputTokens", 0)
            stop_reason   = response.get("stopReason", "end_turn")

            # Datadog LLM Observability reads gen_ai.* span attributes
            s.set_attribute("gen_ai.usage.input_tokens",      input_tokens)
            s.set_attribute("gen_ai.usage.output_tokens",     output_tokens)
            s.set_attribute("gen_ai.response.finish_reasons", stop_reason)

            logger.info(
                "Bedrock Converse completed",
                extra={
                    "model_id":      self.model_id,
                    "input_tokens":  input_tokens,
                    "output_tokens": output_tokens,
                    "stop_reason":   stop_reason,
                },
            )

            content_blocks = response.get("output", {}).get("message", {}).get("content", [])
            for block in content_blocks:
                if block.get("toolUse", {}).get("name") == "classify_email":
                    return json.dumps(block["toolUse"]["input"]), input_tokens, output_tokens

        raise LLMInvocationError("Bedrock response contained no classify_email tool_use block")
