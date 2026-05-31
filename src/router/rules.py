# -*- coding: utf-8 -*-
"""
Routing rule engine.

Rules are evaluated in priority order (lowest number = highest priority).
The first matching rule wins.

Review fixes applied
--------------------
P37 — ROUTING_RULES pre-sorted once at module load; route() no longer pays
       the sort cost on every invocation.
P38 — Removed the inaccurate docstring claim that "rules are loaded from the
       datastore at cold start".  Rules are static lambdas defined here.
       Datastore-driven rules would require a separate loader and are not
       implemented.
P44 — "email_forward" destination added to keep rules.py and destinations.py
       in sync (RoutingDecision.destination docs listed it; it was absent from
       DESTINATION_HANDLERS).  email_forward is wired in destinations.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from src.classifier.models import ClassificationResult, Intent, Urgency

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Output of the rule engine — consumed by the dispatcher."""

    destination:     str   # "slack" | "hubspot" | "linear" | "email_forward" | "human_queue" | "archive"
    channel_or_queue: str  # e.g. "#sales", "#support", "human-review"
    create_ticket:   bool  = False
    notify_owner:    bool  = False
    metadata:        dict  = field(default_factory=dict)


@dataclass
class RoutingRule:
    """A single routing rule with a priority, a predicate, and an action."""

    priority: int
    name:     str
    match:    Callable[[ClassificationResult], bool]
    action:   Callable[[ClassificationResult], RoutingDecision]


# ── Rule definitions ──────────────────────────────────────────────────────────

_RULES: list[RoutingRule] = [

    # P1 — Urgent escalations → #incidents + notify owner
    RoutingRule(
        priority=10,
        name="urgent_escalation",
        match=lambda r: r.intent == Intent.URGENT_ESCALATION,
        action=lambda r: RoutingDecision(
            destination="slack",
            channel_or_queue="#incidents",
            create_ticket=True,
            notify_owner=True,
            metadata={"urgency": r.urgency.value, "sentiment": r.sentiment.value},
        ),
    ),

    # P2 — CRITICAL urgency on any intent → #incidents + notify owner
    RoutingRule(
        priority=20,
        name="critical_urgency",
        match=lambda r: r.urgency == Urgency.CRITICAL,
        action=lambda r: RoutingDecision(
            destination="slack",
            channel_or_queue="#incidents",
            notify_owner=True,
            metadata={"intent": r.intent.value, "summary": r.summary},
        ),
    ),

    # P3 — Human review for low-confidence or unknown intent
    RoutingRule(
        priority=30,
        name="human_review",
        match=lambda r: r.requires_human or r.intent == Intent.UNKNOWN,
        action=lambda r: RoutingDecision(
            destination="human_queue",
            channel_or_queue="human-review",
            metadata={"confidence": r.confidence, "reasoning": r.reasoning},
        ),
    ),

    # P10 — Sales inquiry → HubSpot deal + Slack #sales
    RoutingRule(
        priority=100,
        name="sales_inquiry",
        match=lambda r: r.intent == Intent.SALES_INQUIRY,
        action=lambda r: RoutingDecision(
            destination="hubspot",
            channel_or_queue="#sales",
            metadata={"summary": r.summary, "urgency": r.urgency.value},
        ),
    ),

    # P11 — Support request → Linear issue + Slack #support
    RoutingRule(
        priority=110,
        name="support_request",
        match=lambda r: r.intent == Intent.SUPPORT_REQUEST,
        action=lambda r: RoutingDecision(
            destination="linear",
            channel_or_queue="#support",
            create_ticket=True,
            metadata={"order_id": r.order_id, "urgency": r.urgency.value},
        ),
    ),

    # P12 — Billing question → Slack #billing; DM owner if high/critical urgency
    RoutingRule(
        priority=120,
        name="billing_question",
        match=lambda r: r.intent == Intent.BILLING_QUESTION,
        action=lambda r: RoutingDecision(
            destination="slack",
            channel_or_queue="#billing",
            notify_owner=r.urgency in (Urgency.HIGH, Urgency.CRITICAL),
            metadata={"order_id": r.order_id},
        ),
    ),

    # P13 — Vendor outreach → Slack #vendors
    RoutingRule(
        priority=130,
        name="vendor_outreach",
        match=lambda r: r.intent == Intent.VENDOR_OUTREACH,
        action=lambda r: RoutingDecision(
            destination="slack",
            channel_or_queue="#vendors",
            metadata={"summary": r.summary},
        ),
    ),

    # P14 — Job application → email forward + Slack #hiring (P44)
    RoutingRule(
        priority=140,
        name="job_application",
        match=lambda r: r.intent == Intent.JOB_APPLICATION,
        action=lambda r: RoutingDecision(
            destination="email_forward",
            channel_or_queue="#hiring",
            metadata={"sender": r.sender_name},
        ),
    ),

    # P99 — Marketing noise → archive (no notification)
    RoutingRule(
        priority=990,
        name="marketing_noise",
        match=lambda r: r.intent == Intent.MARKETING_NOISE,
        action=lambda r: RoutingDecision(
            destination="archive",
            channel_or_queue="archived",
        ),
    ),

    # P100 — Catch-all fallback → human queue
    RoutingRule(
        priority=1000,
        name="fallback",
        match=lambda r: True,
        action=lambda r: RoutingDecision(
            destination="human_queue",
            channel_or_queue="human-review",
            metadata={"intent": r.intent.value, "confidence": r.confidence},
        ),
    ),
]

# P37: sorted once at module load — not on every route() call
ROUTING_RULES: list[RoutingRule] = sorted(_RULES, key=lambda r: r.priority)


def route(result: ClassificationResult) -> RoutingDecision:
    """
    Evaluate rules in priority order and return the first matching decision.
    Always returns a decision — the catch-all fallback guarantees this.
    """
    for rule in ROUTING_RULES:  # already sorted (P37)
        if rule.match(result):
            decision = rule.action(result)
            logger.info(
                "Rule matched",
                extra={
                    "rule":        rule.name,
                    "destination": decision.destination,
                    "channel":     decision.channel_or_queue,
                },
            )
            return decision

    # Unreachable — catch-all fallback always matches
    raise RuntimeError("No routing rule matched — missing catch-all fallback")
