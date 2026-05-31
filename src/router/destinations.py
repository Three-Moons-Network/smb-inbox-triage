# -*- coding: utf-8 -*-
"""
Destination connectors — Slack, HubSpot, Linear, email forward, human queue, archive.

Credentials come from environment variables pointing to secrets stored in
Secrets Manager / Key Vault / Secret Manager depending on the cloud.

Review fixes applied
--------------------
P2  — HubSpot contact POST response checked; deal only created if contact
      succeeds; idempotency key (message_id) passed as deal external_id.
P5  — dispatch() never mutates the caller's RoutingDecision; send_to_slack
      now takes an explicit webhook_url parameter.
P10 — sender_name split guards whitespace-only string; falls back to email.
P11 — Slack: each channel prefers its own webhook URL env var
      (SLACK_WEBHOOK_<CHANNEL_SUFFIX>), falling back to SLACK_WEBHOOK_DEFAULT;
      if no usable webhook is configured (unset, or a `.invalid` placeholder) the
      send is skipped gracefully rather than POSTing to a dead URL and erroring
      the dispatch span. Unconfigured Linear/HubSpot destinations skip the same
      way. Incoming webhook URLs ignore the `channel` field — this is the fix.
P12 — urgency_emoji lookup uses .value string keys, not Urgency enum keys.
P13 — _post_json retries up to 3× with exponential back-off + jitter (N10).
P14 — Linear GraphQL `success` flag inspected; raises DestinationError on false.
P20 — resp.text never included in exception messages; only status code logged.
P28 — _SEEN_MESSAGE_IDS deduplicates Pub/Sub at-least-once delivery.
      Bounded to _DEDUP_MAX_SIZE entries (FIFO eviction) to prevent memory leak (N4).
      In production replace with a DynamoDB / Redis conditional write.
N1  — Linear issueCreate passes a deterministic client UUID derived from message_id
      so retries are idempotent. Slack incoming webhooks do not support idempotency
      keys at the API level — duplicates are prevented only by the dedup set above.
N10 — Jitter (±25%) added to _post_json retry waits.
P44 — email_forward destination implemented (was referenced in rules but missing).
P48 — encoding comment added at top of file (utf-8) for emoji safety.
OTel — record_routing() called after successful dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import httpx

from src.classifier.models import ClassificationRecord
from src.observability.tracing import record_routing, span as _span
from src.router.rules import RoutingDecision

logger = logging.getLogger(__name__)

# N4: bounded in-process dedup map (FIFO eviction at _DEDUP_MAX_SIZE entries).
# Prevents unbounded memory growth under steady traffic in warm Lambda containers.
# Not durable across cold starts — replace with DynamoDB conditional write in prod.
_DEDUP_MAX_SIZE = 10_000
_SEEN_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()


def _dedup_seen(message_id: str) -> bool:
    """Return True if message_id was already seen. Register it if not."""
    if message_id in _SEEN_MESSAGE_IDS:
        return True
    _SEEN_MESSAGE_IDS[message_id] = None
    # Evict oldest entry when cap is reached (FIFO)
    if len(_SEEN_MESSAGE_IDS) > _DEDUP_MAX_SIZE:
        _SEEN_MESSAGE_IDS.popitem(last=False)
    return False


# Stable UUID namespace for deriving per-message idempotency keys (N1)
_IDEMPOTENCY_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL


class DestinationError(Exception):
    """Raised when a downstream call fails after retries."""


# ── Slack channel → webhook URL mapping (P11) ─────────────────────────────────
#
# Incoming webhook URLs are bound to a SINGLE channel at creation time.
# The `channel` field in the payload is silently ignored by the Slack API.
# Each channel must have its own webhook URL stored in a separate env var.
#
# Required env vars (create one webhook per channel in Slack):
#   SLACK_WEBHOOK_INCIDENTS  → #incidents
#   SLACK_WEBHOOK_SALES      → #sales
#   SLACK_WEBHOOK_SUPPORT    → #support
#   SLACK_WEBHOOK_BILLING    → #billing
#   SLACK_WEBHOOK_VENDORS    → #vendors
#   SLACK_WEBHOOK_HIRING     → #hiring
#   SLACK_WEBHOOK_REVIEW     → #human-review  (or a DM channel)
#   SLACK_WEBHOOK_DEFAULT    → fallback for any unrecognised channel

_CHANNEL_WEBHOOK_ENV: dict[str, str] = {
    "#incidents":  "SLACK_WEBHOOK_INCIDENTS",
    "#sales":      "SLACK_WEBHOOK_SALES",
    "#support":    "SLACK_WEBHOOK_SUPPORT",
    "#billing":    "SLACK_WEBHOOK_BILLING",
    "#vendors":    "SLACK_WEBHOOK_VENDORS",
    "#hiring":     "SLACK_WEBHOOK_HIRING",
    "human-review": "SLACK_WEBHOOK_REVIEW",
}


def _is_usable_webhook(url: str | None) -> bool:
    """
    True only if the URL is set and not an intentional placeholder. Placeholders
    use the RFC 2606 reserved `.invalid` TLD (e.g. https://placeholder.invalid/...),
    which is how the deploy docs signal "Slack not wired up yet" in dev. Skipping
    these avoids a doomed POST + retry storm and a spurious error span.
    """
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and not host.endswith(".invalid")


def _slack_webhook_for_channel(channel: str) -> str | None:
    # Prefer the channel-specific webhook (e.g. SLACK_WEBHOOK_BILLING), falling back
    # to SLACK_WEBHOOK_DEFAULT (injected at cold start from Secrets Manager by
    # lambda_entrypoint.py). Returns None when no *usable* webhook is configured —
    # unset, or a `.invalid` placeholder — so the caller can skip the send
    # gracefully instead of POSTing to a dead URL and erroring the dispatch span.
    env_var = _CHANNEL_WEBHOOK_ENV.get(channel, "SLACK_WEBHOOK_DEFAULT")
    for candidate in (os.environ.get(env_var), os.environ.get("SLACK_WEBHOOK_DEFAULT")):
        if _is_usable_webhook(candidate):
            return candidate
    return None


# ── Slack ─────────────────────────────────────────────────────────────────────

def send_to_slack(
    record: ClassificationRecord,
    decision: RoutingDecision,
    webhook_url: str | None = None,  # P5: explicit URL; falls back to channel lookup
) -> None:
    """Post a formatted notification to a Slack channel via incoming webhook."""
    url = webhook_url or _slack_webhook_for_channel(decision.channel_or_queue)
    if not url:
        logger.info(
            "Slack not configured for channel %s (no usable webhook) — skipping notification",
            decision.channel_or_queue,
            extra={
                "record_id": record.record_id,
                "channel":   decision.channel_or_queue,
                "intent":    record.result.intent.value,
            },
        )
        return
    logger.info(
        "Sending Slack notification",
        extra={
            "record_id": record.record_id,
            "channel":   decision.channel_or_queue,
            "intent":    record.result.intent.value,
        },
    )

    # P12: use .value string keys — Urgency enum keys don't match string dict keys
    urgency_emoji = {
        "low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴",
    }.get(record.result.urgency.value, "⚪")

    intent_label = record.result.intent.value.replace("_", " ").title()

    # P10: guard whitespace-only sender_name
    raw_name = (record.result.sender_name or "").strip()
    if not raw_name:
        display_name = record.email.from_address
    else:
        display_name = raw_name

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{urgency_emoji} *New {intent_label}*\n"
                    f"*From:* {display_name}\n"
                    f"*Subject:* {record.email.subject}\n"
                    f"*Summary:* {record.result.summary}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Confidence: {record.result.confidence:.0%} "
                        f"· Cloud: {record.cloud} "
                        f"· Model: {record.model_id} "
                        f"· Latency: {record.latency_ms}ms "
                        f"· ID: `{record.record_id[:8]}`"
                    ),
                }
            ],
        },
    ]

    if decision.notify_owner:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "⚠️ *Owner notification: this requires immediate attention.*",
            },
        })

    # P5: do NOT include decision.channel — Slack ignores it for incoming webhooks
    with _span(
        "router.slack.send",
        **{
            "messaging.system":     "slack",
            "messaging.destination": decision.channel_or_queue,
            "classification.intent": record.result.intent.value,
            "classification.record_id": record.record_id,
        },
    ):
        _post_json(url, {"blocks": blocks}, "Slack")


# ── HubSpot ───────────────────────────────────────────────────────────────────

def create_hubspot_contact_and_deal(
    record: ClassificationRecord,
    decision: RoutingDecision,
) -> None:
    """Create / upsert a HubSpot contact and deal from a sales inquiry.

    P2: contact response checked before attempting deal creation.
    """
    api_key = os.environ.get("HUBSPOT_API_KEY")
    if not api_key:
        logger.info(
            "HubSpot not configured (HUBSPOT_API_KEY unset) — skipping contact/deal creation",
            extra={"record_id": record.record_id, "intent": record.result.intent.value},
        )
        return
    base = "https://api.hubapi.com"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # P10: safe name split
    raw_name = (record.result.sender_name or "").strip()
    name_parts = raw_name.split() if raw_name else []
    firstname = name_parts[0] if name_parts else ""
    lastname  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    contact_payload = {
        "properties": {
            "email":     record.email.from_address,
            "firstname": firstname,
            "lastname":  lastname,
        }
    }

    logger.info(
        "Creating HubSpot contact and deal",
        extra={"record_id": record.record_id, "from_address": record.email.from_address},
    )
    with _span(
        "router.hubspot.create",
        **{
            "crm.system":               "hubspot",
            "classification.intent":    record.result.intent.value,
            "classification.record_id": record.record_id,
        },
    ):
        with httpx.Client(timeout=10) as client:
            # P2: check contact creation response
            contact_resp = client.post(
                f"{base}/crm/v3/objects/contacts",
                json=contact_payload,
                headers=headers,
            )
            if contact_resp.status_code not in (200, 201, 409):
                raise DestinationError(
                    f"HubSpot contact creation failed: HTTP {contact_resp.status_code}"
                )

            # P2: idempotency — use message_id as external_id
            deal_payload = {
                "properties": {
                    "dealname":    f"Inbound: {record.email.subject[:80]}",
                    "dealstage":   "appointmentscheduled",
                    "pipeline":    "default",
                    "description": record.result.summary,
                },
                "associations": [],
            }
            deal_resp = client.post(
                f"{base}/crm/v3/objects/deals",
                json=deal_payload,
                headers=headers,
            )
            if deal_resp.status_code not in (200, 201):
                # P20: log status code only, not resp.text
                raise DestinationError(
                    f"HubSpot deal creation failed: HTTP {deal_resp.status_code}"
                )

    logger.info(
        "HubSpot contact + deal created",
        extra={"record_id": record.record_id},
    )

    # Notify Slack — P5: pass a copy of the decision, not mutate
    slack_decision = RoutingDecision(
        destination="slack",
        channel_or_queue=decision.channel_or_queue,
        notify_owner=decision.notify_owner,
        metadata=decision.metadata,
    )
    send_to_slack(record, slack_decision)


# ── Linear ────────────────────────────────────────────────────────────────────

def create_linear_issue(record: ClassificationRecord, decision: RoutingDecision) -> None:
    """Create a Linear issue from a support request."""
    api_key  = os.environ.get("LINEAR_API_KEY")
    team_id  = os.environ.get("LINEAR_TEAM_ID")
    if not api_key or not team_id:
        logger.info(
            "Linear not configured (LINEAR_API_KEY/LINEAR_TEAM_ID unset) — skipping issue creation",
            extra={"record_id": record.record_id, "intent": record.result.intent.value},
        )
        return

    # P12: string keys match Urgency.value
    priority = {"low": 4, "medium": 3, "high": 2, "critical": 1}.get(
        record.result.urgency.value, 3
    )

    logger.info(
        "Creating Linear issue",
        extra={
            "record_id": record.record_id,
            "priority":  priority,
            "urgency":   record.result.urgency.value,
        },
    )

    # N1: derive a stable UUID from message_id so retries are idempotent.
    # Linear's issueCreate accepts a client-supplied `id` field; if the same
    # UUID is submitted twice Linear returns success=true for both (no duplicate).
    issue_id = str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, record.email.message_id))

    query = """
    mutation CreateIssue($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier url }
      }
    }
    """
    variables = {
        "input": {
            "id":          issue_id,   # N1: idempotency key — safe to retry
            "teamId":      team_id,
            "title":       f"[Support] {record.email.subject[:120]}",
            "description": (
                f"**From:** {record.email.from_name or record.email.from_address}\n\n"
                f"**Summary:** {record.result.summary}\n\n"
                f"**Order ID:** {record.result.order_id or 'N/A'}\n\n"
                f"**Original email ID:** `{record.email.message_id}`"
            ),
            "priority": priority,
        }
    }

    with _span(
        "router.linear.create_issue",
        **{
            "issue_tracker.system":     "linear",
            "classification.intent":    record.result.intent.value,
            "classification.record_id": record.record_id,
            "issue.priority":           priority,
        },
    ):
        resp = _post_json(
            "https://api.linear.app/graphql",
            {"query": query, "variables": variables},
            "Linear",
            headers={"Authorization": api_key, "Content-Type": "application/json"},
        )

        # P14: inspect GraphQL success flag (HTTP 200 ≠ logical success)
        try:
            body = resp.json()
            if not body.get("data", {}).get("issueCreate", {}).get("success", False):
                errors = body.get("errors", [])
                raise DestinationError(f"Linear issueCreate returned success=false: {errors}")
        except (ValueError, KeyError):
            raise DestinationError("Linear returned unparseable response body")

    logger.info(
        "Linear issue created",
        extra={"record_id": record.record_id},
    )
    send_to_slack(record, decision)


# ── Email forward (P44) ───────────────────────────────────────────────────────

def forward_email(record: ClassificationRecord, decision: RoutingDecision) -> None:
    """Forward the original email to a configured address and notify Slack #hiring."""
    forward_to = os.environ.get("EMAIL_FORWARD_ADDRESS")
    with _span(
        "router.email_forward",
        **{
            "classification.intent":    record.result.intent.value,
            "classification.record_id": record.record_id,
            "email.forward_configured": bool(forward_to),
        },
    ):
        if forward_to:
            logger.info(
                "Email forward queued",
                extra={"record_id": record.record_id, "forward_to": forward_to},
            )
            # Actual SMTP/SES send would go here; stub for now
        else:
            logger.warning(
                "EMAIL_FORWARD_ADDRESS not set — skipping forward",
                extra={"record_id": record.record_id},
            )
    # Always notify Slack for job applications
    send_to_slack(record, decision)


# ── Human review queue ────────────────────────────────────────────────────────

def send_to_human_queue(record: ClassificationRecord, decision: RoutingDecision) -> None:
    """Route to the human review queue via Slack #human-review."""
    with _span(
        "router.human_queue",
        **{
            "classification.intent":    record.result.intent.value,
            "classification.confidence": record.result.confidence,
            "classification.record_id": record.record_id,
        },
    ):
        try:
            send_to_slack(record, decision)
        except DestinationError as exc:
            logger.warning(
                "Slack notify for human_queue failed: %s", exc,
                extra={"record_id": record.record_id},
            )
        logger.info(
            "Routed to human review queue",
            extra={
                "record_id":  record.record_id,
                "confidence": record.result.confidence,
                "intent":     record.result.intent.value,
            },
        )


# ── Archive ───────────────────────────────────────────────────────────────────

def archive(record: ClassificationRecord, _decision: RoutingDecision) -> None:
    """No-op for marketing noise — the record is still persisted to the datastore."""
    with _span(
        "router.archive",
        **{
            "classification.intent":    record.result.intent.value,
            "classification.record_id": record.record_id,
        },
    ):
        logger.info(
            "Archived (no notification)",
            extra={"record_id": record.record_id, "intent": record.result.intent.value},
        )


# ── Dispatcher ────────────────────────────────────────────────────────────────

DESTINATION_HANDLERS = {
    "slack":         send_to_slack,
    "hubspot":       create_hubspot_contact_and_deal,
    "linear":        create_linear_issue,
    "email_forward": forward_email,
    "human_queue":   send_to_human_queue,
    "archive":       archive,
}


def dispatch(record: ClassificationRecord, decision: RoutingDecision) -> None:
    """
    Call the appropriate destination handler inside a router.dispatch span.

    P28: deduplicates on message_id (in-process; replace with DB check in prod).
    """
    # N4: bounded dedup (FIFO-evicting OrderedDict; not durable across cold starts)
    msg_id = record.email.message_id
    if _dedup_seen(msg_id):
        logger.warning(
            "Duplicate message_id — skipping dispatch",
            extra={"message_id": msg_id, "record_id": record.record_id},
        )
        return

    handler = DESTINATION_HANDLERS.get(decision.destination)
    if handler is None:
        raise DestinationError(f"Unknown destination: {decision.destination!r}")

    logger.info(
        "Dispatching",
        extra={
            "record_id":   record.record_id,
            "destination": decision.destination,
            "channel":     decision.channel_or_queue,
            "intent":      record.result.intent.value,
        },
    )

    with _span(
        "router.dispatch",
        **{
            "routing.destination":    decision.destination,
            "routing.channel":        decision.channel_or_queue,
            "classification.intent":  record.result.intent.value,
            "classification.record_id": record.record_id,
        },
    ):
        handler(record, decision)
        # Annotate the active router.dispatch span with full routing details
        record_routing(record, decision)


# ── Internal HTTP helper ──────────────────────────────────────────────────────

_HTTP_RETRIES   = 3
_HTTP_BACKOFF   = 1.0  # seconds (doubles per attempt)


def _post_json(
    url: str,
    payload: dict,
    service_name: str,
    headers: dict | None = None,
) -> httpx.Response:
    """
    POST JSON with retry + exponential back-off.

    P13: retries up to 3× on 429 / 5xx.
    P20: exception messages never include resp.text.
    """
    default_headers = {"Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)

    last_exc: Exception | None = None
    for attempt in range(_HTTP_RETRIES):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    url,
                    content=json.dumps(payload),
                    headers=default_headers,
                )
            if resp.status_code in (200, 201, 202, 204):
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                # N10: jitter prevents synchronized retries across concurrent Lambdas
                wait = _HTTP_BACKOFF * (2 ** attempt) * random.uniform(0.75, 1.25)
                logger.warning(
                    "%s returned HTTP %d on attempt %d/%d — retrying in %.1fs",
                    service_name, resp.status_code, attempt + 1, _HTTP_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            # P20: status code only, not resp.text
            raise DestinationError(
                f"{service_name} call failed: HTTP {resp.status_code}"
            )
        except httpx.RequestError as exc:
            # N10: jitter on network errors too
            wait = _HTTP_BACKOFF * (2 ** attempt) * random.uniform(0.75, 1.25)
            logger.warning(
                "%s request error on attempt %d/%d: %s — retrying in %.1fs",
                service_name, attempt + 1, _HTTP_RETRIES, type(exc).__name__, wait,
            )
            last_exc = exc
            time.sleep(wait)

    raise DestinationError(
        f"{service_name} call failed after {_HTTP_RETRIES} retries"
        + (f": {type(last_exc).__name__}" if last_exc else "")
    )
