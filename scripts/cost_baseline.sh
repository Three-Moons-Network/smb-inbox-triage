#!/usr/bin/env bash
#
# Phase 3 — capture cost baseline before deploy, then re-run 7 days after.
#
# Pulls last 14 days of email-triage-related cost from each cloud's billing
# API, writes JSON files. Re-run with --post after the deploy + a week of
# traffic to produce a delta.
#
# Required CLIs: aws, gcloud, az.  Tag the cloud resources with:
#   service=email-triage   owner=three-moons-network
#
# Usage:
#   ./cost_baseline.sh --baseline      (run BEFORE deploy)
#   ./cost_baseline.sh --post          (run 7 days AFTER deploy)
#   ./cost_baseline.sh --diff          (compute delta)
#
set -euo pipefail

MODE="${1:---baseline}"
OUT_DIR="$(dirname "$0")/../cost"
mkdir -p "$OUT_DIR"

TODAY=$(date -u +%Y-%m-%d)
START_14D_AGO=$(date -u -d "14 days ago" +%Y-%m-%d 2>/dev/null || date -u -v -14d +%Y-%m-%d)

LABEL="baseline"
[[ "$MODE" == "--post" ]] && LABEL="post"

echo "# cost capture mode=$MODE label=$LABEL  window=$START_14D_AGO -> $TODAY"

# ----- AWS Cost Explorer -----
echo "## AWS"
aws ce get-cost-and-usage \
  --time-period Start="$START_14D_AGO",End="$TODAY" \
  --granularity DAILY \
  --metrics UnblendedCost \
  --group-by Type=TAG,Key=service \
  --filter '{"Tags":{"Key":"service","Values":["email-triage"]}}' \
  > "$OUT_DIR/aws-${LABEL}.json" || {
    echo "  ! aws ce call failed — confirm Cost Explorer is enabled + tag is active"
  }
echo "  -> $OUT_DIR/aws-${LABEL}.json"

# ----- GCP Billing -----
# Requires a BigQuery billing export. Adjust dataset/table names.
echo "## GCP"
GCP_BILLING_TABLE="${GCP_BILLING_TABLE:-PROJECT.dataset.gcp_billing_export_v1_BILLING_ID}"
gcloud_query="SELECT
  service.description AS service,
  SUM(cost) AS cost
FROM \`${GCP_BILLING_TABLE}\`
WHERE DATE(usage_start_time) BETWEEN '${START_14D_AGO}' AND '${TODAY}'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = 'service' AND l.value = 'email-triage')
GROUP BY 1
ORDER BY cost DESC"
bq query --format=json --use_legacy_sql=false "$gcloud_query" \
  > "$OUT_DIR/gcp-${LABEL}.json" 2>/dev/null || {
    echo "  ! bq query failed — set GCP_BILLING_TABLE env var to your billing export"
  }
echo "  -> $OUT_DIR/gcp-${LABEL}.json"

# ----- Azure Cost Management -----
echo "## Azure"
RG="${AZURE_RG:-email-triage-rg}"
az costmanagement query \
  --scope "/subscriptions/$(az account show --query id -o tsv)/resourceGroups/${RG}" \
  --type ActualCost \
  --timeframe Custom \
  --time-period from="${START_14D_AGO}T00:00:00Z" to="${TODAY}T00:00:00Z" \
  --dataset-granularity Daily \
  > "$OUT_DIR/azure-${LABEL}.json" || {
    echo "  ! az costmanagement call failed — check the resource group exists"
  }
echo "  -> $OUT_DIR/azure-${LABEL}.json"

if [[ "$MODE" == "--diff" ]]; then
  echo
  echo "# delta (post - baseline):"
  for cloud in aws gcp azure; do
    base="$OUT_DIR/${cloud}-baseline.json"
    post="$OUT_DIR/${cloud}-post.json"
    if [[ -f "$base" && -f "$post" ]]; then
      echo "## ${cloud}"
      # crude sum-of-costs extractor — refine per cloud's exact JSON shape
      python3 - <<PY
import json, pathlib
def total(path):
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except Exception:
        return None
    s = 0.0
    def walk(o):
        nonlocal s
        if isinstance(o, dict):
            for k,v in o.items():
                if k.lower() in ("cost","amount","unblendedcost","total") and isinstance(v,(int,float)):
                    s += float(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(data)
    return s
print(f"  baseline=\${total('$base'):.2f}   post=\${total('$post'):.2f}")
PY
    fi
  done
fi

echo
echo "done. files in