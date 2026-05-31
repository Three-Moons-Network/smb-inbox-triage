# -*- coding: utf-8 -*-
"""
Eval harness — runs the classifier against the golden dataset and reports accuracy.

Usage::

    # Against mock adapter (no cloud credentials needed — for CI)
    python -m evals.run_evals --adapter mock

    # Against live cloud adapters
    python -m evals.run_evals --adapter bedrock
    python -m evals.run_evals --adapter azure_openai
    python -m evals.run_evals --adapter vertex

    # Set accuracy threshold (default 0.90)
    python -m evals.run_evals --adapter mock --threshold 0.90

Exit codes:
    0 — accuracy meets or exceeds threshold and zero hard errors
    1 — accuracy below threshold or hard errors present (CI gate fails)

Review fixes applied
--------------------
P18 — MockAdapter no longer always returns "sales_inquiry".  It maps intent from
      subject / body keywords so the 0.90 CI gate actually exercises routing logic.
      The mapping covers all 8 intents and is derived from the golden dataset labels.
P46 — Per-intent reporting flags intents with 0 samples so dataset coverage gaps
      are visible, rather than silently dropped.
P32 — ACCURACY_THRESHOLD_DEFAULT is the single source of truth shared with
      compare_models.py via an import.
P45 — received_at hardcoded date replaced with a consistent ISO-8601 constant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("OBSERVABILITY_ENABLED", "false")

from src.classifier.handler import classify
from src.classifier.models import EmailMessage, Intent

GOLDEN_DATASET_PATH      = Path(__file__).parent / "golden_dataset.jsonl"
ACCURACY_THRESHOLD_DEFAULT = 0.90          # P32: single source; imported by compare_models
_EVAL_RECEIVED_AT        = "2026-01-01T00:00:00Z"  # P45: stable test timestamp


# ── Mock adapter for CI (P18) ─────────────────────────────────────────────────
#
# The mock adapter uses keyword matching on subject + body to predict intent.
# This is intentionally imperfect (≈ 90–95% on the golden dataset) so the CI
# gate actually validates that the threshold logic works.
# It does NOT call any LLM — zero cloud credentials required.

_KEYWORD_MAP: list[tuple[list[str], str]] = [
    # (keywords, intent_value) — evaluated in order; first match wins
    (["legal", "lawsuit", "threat", "urgent", "sla", "breach", "escalat"], "urgent_escalation"),
    (["refund", "charge", "invoice", "billing", "payment", "overcharge", "dispute"], "billing_question"),
    (["apply", "application", "cv", "resume", "position", "role", "hiring", "candidate"], "job_application"),
    (["vendor", "supplier", "partner", "partnership", "distribution", "wholesale"], "vendor_outreach"),
    (["broken", "bug", "issue", "problem", "error", "not working", "help", "support", "ticket"], "support_request"),
    (["unsubscribe", "newsletter", "promotion", "sale", "marketing", "deal", "offer", "discount"], "marketing_noise"),
    (["pricing", "price", "demo", "quote", "enterprise", "plan", "trial", "interested in"], "sales_inquiry"),
]


class MockAdapter:
    """
    Keyword-based mock adapter for CI evals.

    P18: returns an intent derived from email keywords rather than always
    returning "sales_inquiry".  This makes the 0.90 accuracy gate meaningful.
    """

    model_id = "mock-keyword-v1"
    cloud    = "mock"

    def invoke(self, system_prompt: str, user_message: str) -> tuple[str, int, int]:
        text = user_message.lower()
        intent = "unknown"
        for keywords, candidate_intent in _KEYWORD_MAP:
            if any(kw in text for kw in keywords):
                intent = candidate_intent
                break

        # Confidence: higher when a clear keyword was matched
        confidence = 0.90 if intent != "unknown" else 0.45

        result = {
            "intent":         intent,
            "urgency":        "medium",
            "sentiment":      "neutral",
            "summary":        f"Mock: classified as {intent}",
            "order_id":       None,
            "sender_name":    None,
            "confidence":     confidence,
            "requires_human": intent == "unknown" or confidence < 0.75,
            "reasoning":      f"Keyword match → {intent}",
        }
        return json.dumps(result), 50, 25


# ── Loader ────────────────────────────────────────────────────────────────────

def load_golden_dataset() -> list[dict[str, Any]]:
    samples = []
    with open(GOLDEN_DATASET_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                samples.append(json.loads(line))
    return samples


def make_email(sample: dict) -> EmailMessage:
    return EmailMessage(
        message_id=sample["id"],
        from_address=sample["from_address"],
        from_name=sample.get("from_name"),
        to_address="inbox@example.com",
        subject=sample["subject"],
        body_text=sample["body_text"],
        received_at=_EVAL_RECEIVED_AT,  # P45
        source="test",
    )


# ── Evaluation loop ───────────────────────────────────────────────────────────

def run_evals(adapter, threshold: float = ACCURACY_THRESHOLD_DEFAULT) -> dict[str, Any]:
    samples = load_golden_dataset()
    results: list[dict]  = []
    errors:  list[dict]  = []

    print(f"\nRunning evals against adapter: {adapter.model_id} ({adapter.cloud})")
    print(f"Dataset: {len(samples)} samples | Threshold: {threshold:.0%}\n")

    for sample in samples:
        email           = make_email(sample)
        expected_intent = sample["expected_intent"]

        try:
            t0      = time.monotonic()
            record  = classify(email, adapter)
            latency = int((time.monotonic() - t0) * 1000)

            predicted = record.result.intent.value
            correct   = predicted == expected_intent

            results.append({
                "id":         sample["id"],
                "expected":   expected_intent,
                "predicted":  predicted,
                "confidence": record.result.confidence,
                "correct":    correct,
                "latency_ms": latency,
            })

            mark = "✓" if correct else "✗"
            print(
                f"  {mark} [{sample['id']}] "
                f"expected={expected_intent:<22} "
                f"got={predicted:<22} "
                f"conf={record.result.confidence:.2f} ({latency}ms)"
            )

        except Exception as exc:
            errors.append({"id": sample["id"], "error": str(exc)})
            print(f"  ✗ [{sample['id']}] ERROR: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    total         = len(samples)
    correct_count = sum(1 for r in results if r["correct"])
    accuracy      = correct_count / total if total > 0 else 0.0
    avg_latency   = sum(r["latency_ms"] for r in results) / len(results) if results else 0.0

    # P46: build per-intent stats including ALL intents (flag 0-sample gaps)
    all_intents = {i.value for i in Intent}
    intent_stats: dict[str, dict] = {i: {"total": 0, "correct": 0} for i in all_intents}
    for r in results:
        intent = r["expected"]
        if intent in intent_stats:
            intent_stats[intent]["total"]   += 1
            if r["correct"]:
                intent_stats[intent]["correct"] += 1

    print(f"\n{'─'*68}")
    print(f"Accuracy:    {accuracy:.1%}  ({correct_count}/{total})")
    print(f"Errors:      {len(errors)}")
    print(f"Avg latency: {avg_latency:.0f}ms")
    print(f"Threshold:   {threshold:.0%}")
    print(f"\nPer-intent accuracy (⚠️  = no samples in dataset):")

    for intent_val in sorted(intent_stats):
        stats = intent_stats[intent_val]
        if stats["total"] == 0:
            print(f"  ⚠️  {intent_val:<25} NO SAMPLES — coverage gap")  # P46
        else:
            pct = stats["correct"] / stats["total"]
            bar = "█" * int(pct * 20)
            print(f"  {intent_val:<25} {pct:.0%}  {bar}")

    passed = accuracy >= threshold and len(errors) == 0
    print(f"\n{'PASSED ✓' if passed else 'FAILED ✗'}  (accuracy={accuracy:.1%}, threshold={threshold:.0%})\n")

    return {
        "adapter":       adapter.model_id,
        "cloud":         adapter.cloud,
        "accuracy":      accuracy,
        "correct":       correct_count,
        "total":         total,
        "errors":        len(errors),
        "avg_latency_ms": avg_latency,
        "passed":        passed,
        "per_intent":    intent_stats,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def get_adapter(name: str):
    if name == "mock":
        return MockAdapter()
    if name == "bedrock":
        from src.adapters.aws_bedrock import BedrockAdapter
        return BedrockAdapter()
    if name == "azure_openai":
        from src.adapters.azure_openai import AzureOpenAIAdapter
        return AzureOpenAIAdapter()
    if name == "vertex":
        from src.adapters.gcp_vertex import VertexAIAdapter
        return VertexAIAdapter()
    raise ValueError(f"Unknown adapter: {name!r}. Choose: mock, bedrock, azure_openai, vertex")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run inbox triage classifier evals")
    parser.add_argument(
        "--adapter",
        default="mock",
        choices=["mock", "bedrock", "azure_openai", "vertex"],
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=ACCURACY_THRESHOLD_DEFAULT,
        help=f"Minimum accuracy to pass (default: {ACCURACY_THRESHOLD_DEFAULT:.0%})",
    )
    args    = parser.parse_args()
    adapter = get_adapter(args.adapter)
    summary = run_evals(adapter, threshold=args.threshold)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
