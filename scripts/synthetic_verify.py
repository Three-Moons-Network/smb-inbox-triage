#!/usr/bin/env python3
"""
Synthetic verification for email triage deploys.

Sends N requests with distinct trace IDs, waits, then queries Datadog APM /
Logs / Metrics APIs to confirm 10/10 land in each signal within 60s.

Run after each cloud deploy. Pass --url to point at the cloud under test.

Usage:
    export DD_API_KEY=...
    export DD_APP_KEY=...
    export DD_SITE=datadoghq.com   # or your DD site

    # AWS: API Gateway HTTP API
    python synthetic_verify.py --url https://<api-id>.execute-api.<region>.amazonaws.com/webhook --cloud aws
    # GCP: Cloud Run V2 (classifier service URL from terraform output)
    python synthetic_verify.py --url https://<run-url>/ --cloud gcp
    # Azure: Function App
    python synthetic_verify.py --url https://func-smb-inbox-triage-<env>-classifier.azurewebsites.net/api/webhook --cloud azure
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests  # pip install requests

SERVICE = "smb-inbox-triage"


def gen_trace_ids(n: int) -> List[str]:
    # 16-byte hex (128-bit) per W3C trace-context
    return [secrets.token_hex(16) for _ in range(n)]


def fire_requests(url: str, trace_ids: List[str], cloud: str) -> List[Dict[str, Any]]:
    sent: List[Dict[str, Any]] = []
    for tid in trace_ids:
        # 8-byte span ID
        sid = secrets.token_hex(8)
        headers = {
            "content-type": "application/json",
            # W3C tracecontext header: version-traceid-spanid-flags
            "traceparent": f"00-{tid}-{sid}-01",
        }
        # Optional: bearer token for Cloud Run authenticated services / IAP.
        # Set DD_INVOKE_TOKEN (e.g. `gcloud auth print-identity-token`) before
        # running against an authenticated GCP service.
        invoke_token = os.environ.get("DD_INVOKE_TOKEN", "").strip()
        if invoke_token:
            headers["Authorization"] = f"Bearer {invoke_token}"
        # Match the EmailMessage shape that classifier/handler.py expects.
        email_msg = {
            "messageId":   f"synthetic-{tid[:12]}",
            "fromAddress": "synthetic@example.com",
            "fromName":    "Synthetic Verify",
            "toAddress":   "inbox@example.com",
            "subject":     f"synthetic verify {tid[:8]}",
            "bodyText":    "verification request from scripts/synthetic_verify.py",
            "receivedAt":  "",
            "source":      "synthetic_verify",
        }
        if cloud == "gcp":
            # GCP handle_webhook expects a Pub/Sub push envelope:
            # {"message": {"data": "<base64 EmailMessage JSON>"}}
            body = {
                "message": {
                    "data": base64.b64encode(
                        json.dumps(email_msg).encode("utf-8")
                    ).decode("ascii"),
                    "messageId": f"synthetic-{tid[:12]}",
                }
            }
        else:
            # AWS Lambda + Azure Functions accept the flat EmailMessage shape.
            body = email_msg
        t0 = time.monotonic()
        try:
            r = requests.post(url, headers=headers, json=body, timeout=30)
            ok = r.status_code < 400
        except Exception as exc:
            ok, r = False, None
            print(f"  ! request failed: {exc}", file=sys.stderr)
        sent.append({
            "trace_id": tid,
            "ok": ok,
            "status": r.status_code if r is not None else None,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        })
        print(f"  -> {tid[:8]}  status={sent[-1]['status']}  {sent[-1]['elapsed_ms']}ms")
    return sent


def dd_query(path: str, params: Dict[str, Any] = None, body: Any = None) -> Dict[str, Any]:
    site = os.environ.get("DD_SITE", "datadoghq.com")
    base = f"https://api.{site}"
    headers = {
        "DD-API-KEY": os.environ["DD_API_KEY"],
        "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"],
        "content-type": "application/json",
    }
    url = base + path
    if body is None:
        r = requests.get(url, headers=headers, params=params, timeout=30)
    else:
        r = requests.post(url, headers=headers, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def check_traces(trace_ids: List[str], from_ts: int, to_ts: int, cloud: str) -> int:
    # Spans Search API
    body = {
        "data": {
            "type": "search_request",
            "attributes": {
                "filter": {
                    "query": f"service:{SERVICE} @cloud.provider:{cloud}",
                    "from": f"{from_ts}",
                    "to": f"{to_ts}",
                },
                "page": {"limit": 200},
                "sort": "-timestamp",
            },
        }
    }
    resp = dd_query("/api/v2/spans/events/search", body=body)
    found = 0
    seen = {s["attributes"]["trace_id"] for s in resp.get("data", []) if "trace_id" in s["attributes"]}
    for tid in trace_ids:
        if tid in seen:
            found += 1
    return found


def check_logs(trace_ids: List[str], from_ts: int, to_ts: int, cloud: str) -> int:
    body = {
        "filter": {
            "query": f"service:{SERVICE} @cloud.provider:{cloud}",
            "from": f"{from_ts}",
            "to": f"{to_ts}",
        },
        "page": {"limit": 1000},
    }
    resp = dd_query("/api/v2/logs/events/search", body=body)
    seen = {
        l.get("attributes", {}).get("attributes", {}).get("trace_id")
        for l in resp.get("data", [])
    }
    return sum(1 for tid in trace_ids if tid in seen)


def check_metrics(from_ts: int, to_ts: int, cloud: str) -> bool:
    # Confirm any classifier span-derived metric landed during the window.
    # trace.<root_span>.hits is auto-generated by Datadog APM for every traced
    # service — works on all three clouds with the same query.
    query = (
        f'sum:trace.classifier.classify_email.hits'
        f'{{service:{SERVICE}}}.as_count()'
    )
    resp = dd_query("/api/v1/query", params={"from": from_ts, "to": to_ts, "query": query})
    series = resp.get("series") or []
    return any(any(p[1] for p in s.get("pointlist", [])) for s in series)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--cloud", required=True, choices=["aws", "gcp", "azure"])
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--wait", type=int, default=60, help="seconds to wait before querying DD")
    args = p.parse_args()

    print(f"# synthetic verify  cloud={args.cloud}  url={args.url}  n={args.count}")

    for k in ("DD_API_KEY", "DD_APP_KEY"):
        if not os.environ.get(k):
            print(f"missing env: {k}", file=sys.stderr)
            return 2

    trace_ids = gen_trace_ids(args.count)
    from_dt = datetime.now(timezone.utc) - timedelta(seconds=30)
    sent = fire_requests(args.url, trace_ids, args.cloud)
    to_dt = datetime.now(timezone.utc) + timedelta(seconds=args.wait)

    ok_sent = sum(1 for s in sent if s["ok"])
    print(f"# sent {ok_sent}/{args.count}")

    print(f"# waiting {args.wait}s for ingest...")
    time.sleep(args.wait)

    from_ts = int(from_dt.timestamp() * 1000)
    to_ts = int(to_dt.timestamp() * 1000)

    traces_found = check_traces(trace_ids, from_ts, to_ts, args.cloud)
    logs_found = check_logs(trace_ids, from_ts, to_ts, args.cloud)
    metrics_ok = check_metrics(from_ts // 1000, to_ts // 1000, args.cloud)

    print()
    print(f"results for cloud={args.cloud}")
    print(f"  requests OK :  {ok_sent}/{args.count}")
    print(f"  traces in DD:  {traces_found}/{args.count}")
    print(f"  logs in DD  :  {logs_found}/{args.count}")
    print(f"  metrics in DD: {'yes' if metrics_ok else 'no'}")

    pass_traces  = traces_found  >= int(args.count * 0.95)
    pass_logs    = logs_found    >= int(args.count * 0.95)
    pass_metrics = metrics_ok
    ok = pass_traces and pass_logs and pass_metrics
    print(f"\nverdict: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
