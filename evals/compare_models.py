"""
Cross-cloud model comparison — runs the same golden dataset against all three
cloud adapters and outputs a side-by-side accuracy / latency / cost table.

Usage:
    python -m evals.compare_models

Requires all three cloud environments to be configured (credentials, env vars).
Results are written to evals/comparison_results.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evals.run_evals import get_adapter, load_golden_dataset, run_evals

ADAPTERS = ["bedrock", "azure_openai", "vertex"]

# Approximate cost per 1K tokens (input) as of 2025 — update as pricing changes
TOKEN_COST_PER_1K = {
    "bedrock":      {"input": 0.00025, "output": 0.00125},   # Claude 3 Haiku
    "azure_openai": {"input": 0.00015, "output": 0.00060},   # GPT-4o-mini
    "vertex":       {"input": 0.000075, "output": 0.000300}, # Gemini 1.5 Flash
}

# Approx tokens per email classification (adjust from real runs)
AVG_INPUT_TOKENS  = 350
AVG_OUTPUT_TOKENS = 120


def estimate_cost_per_1000_emails(adapter_name: str) -> float:
    costs = TOKEN_COST_PER_1K.get(adapter_name, {"input": 0, "output": 0})
    return (
        (AVG_INPUT_TOKENS  / 1000) * costs["input"]  * 1000 +
        (AVG_OUTPUT_TOKENS / 1000) * costs["output"] * 1000
    )


def run_comparison():
    results = {}
    for adapter_name in ADAPTERS:
        print(f"\n{'='*60}")
        print(f"Testing adapter: {adapter_name}")
        print(f"{'='*60}")
        try:
            adapter = get_adapter(adapter_name)
            summary = run_evals(adapter, threshold=0.90)
            summary["estimated_cost_per_1000_emails"] = estimate_cost_per_1000_emails(adapter_name)
            results[adapter_name] = summary
        except Exception as exc:
            print(f"  SKIPPED: {exc}")
            results[adapter_name] = {"error": str(exc), "skipped": True}

    # ── Print comparison table ────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print("CROSS-CLOUD MODEL COMPARISON")
    print(f"{'='*70}")
    print(f"{'Metric':<30} {'Bedrock/Haiku':>15} {'AzureOAI/mini':>15} {'Vertex/Flash':>15}")
    print(f"{'─'*70}")

    def get(name: str, key: str, fmt: str = "") -> str:
        r = results.get(name, {})
        if r.get("skipped"):
            return "SKIPPED"
        val = r.get(key, "N/A")
        if fmt == "pct" and isinstance(val, float):
            return f"{val:.1%}"
        if fmt == "ms" and isinstance(val, (int, float)):
            return f"{val:.0f}ms"
        if fmt == "usd" and isinstance(val, float):
            return f"${val:.3f}"
        return str(val)

    rows = [
        ("Accuracy",         "accuracy",                       "pct"),
        ("Correct/Total",    "correct",                        ""),
        ("Avg Latency",      "avg_latency_ms",                 "ms"),
        ("Errors",           "errors",                         ""),
        ("Passed Gate",      "passed",                         ""),
        ("Cost/1000 emails", "estimated_cost_per_1000_emails", "usd"),
    ]

    for label, key, fmt in rows:
        row = f"{label:<30}"
        for adapter_name in ADAPTERS:
            row += f" {get(adapter_name, key, fmt):>15}"
        print(row)

    print(f"{'─'*70}")

    # Save results
    output_path = Path(__file__).parent / "comparison_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to: {output_path}")

    # Exit non-zero if any adapter failed its gate
    failed = [name for name, r in results.items() if not r.get("passed", False) and not r.get("skipped")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_comparison())
