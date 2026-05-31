"""Unit tests for the routing rule engine."""
# -*- coding: utf-8 -*-

from __future__ import annotations

import pytest

from src.classifier.models import ClassificationResult, Intent, Urgency, Sentiment
from src.router.rules import route, RoutingDecision


def make_result(**kwargs) -> ClassificationResult:
    defaults = {
        "intent":         Intent.SALES_INQUIRY,
        "urgency":        Urgency.MEDIUM,
        "sentiment":      Sentiment.NEUTRAL,
        "summary":        "Test summary",
        "order_id":       None,
        "sender_name":    "Test User",
        "confidence":     0.95,
        "requires_human": False,
        "reasoning":      "Test reasoning",
    }
    defaults.update(kwargs)
    return ClassificationResult(**defaults)


class TestRoutingRules:

    def test_urgent_escalation_goes_to_incidents(self):
        result   = make_result(intent=Intent.URGENT_ESCALATION)
        decision = route(result)
        assert decision.destination      == "slack"
        assert decision.channel_or_queue == "#incidents"
        assert decision.notify_owner     is True

    def test_critical_urgency_escalates_regardless_of_intent(self):
        result   = make_result(intent=Intent.BILLING_QUESTION, urgency=Urgency.CRITICAL)
        decision = route(result)
        assert decision.channel_or_queue == "#incidents"

    def test_human_review_on_low_confidence(self):
        result   = make_result(confidence=0.5, requires_human=True)
        decision = route(result)
        assert decision.destination == "human_queue"

    def test_unknown_intent_goes_to_human_review(self):
        result   = make_result(intent=Intent.UNKNOWN, requires_human=True)
        decision = route(result)
        assert decision.destination == "human_queue"

    def test_sales_inquiry_goes_to_hubspot(self):
        result   = make_result(intent=Intent.SALES_INQUIRY, confidence=0.92)
        decision = route(result)
        assert decision.destination      == "hubspot"
        assert decision.channel_or_queue == "#sales"

    def test_support_request_creates_ticket(self):
        result   = make_result(intent=Intent.SUPPORT_REQUEST, confidence=0.88)
        decision = route(result)
        assert decision.destination  == "linear"
        assert decision.create_ticket is True

    def test_billing_question_goes_to_billing_channel(self):
        result   = make_result(intent=Intent.BILLING_QUESTION, urgency=Urgency.MEDIUM, confidence=0.85)
        decision = route(result)
        assert decision.destination      == "slack"
        assert decision.channel_or_queue == "#billing"
        assert decision.notify_owner     is False

    def test_billing_high_urgency_notifies_owner(self):
        result   = make_result(intent=Intent.BILLING_QUESTION, urgency=Urgency.HIGH, confidence=0.85)
        decision = route(result)
        assert decision.notify_owner is True

    def test_marketing_noise_goes_to_archive(self):
        result   = make_result(intent=Intent.MARKETING_NOISE, confidence=0.99)
        decision = route(result)
        assert decision.destination == "archive"

    def test_vendor_outreach_goes_to_vendors_channel(self):
        result   = make_result(intent=Intent.VENDOR_OUTREACH, confidence=0.82)
        decision = route(result)
        assert decision.destination      == "slack"
        assert decision.channel_or_queue == "#vendors"

    def test_job_application_goes_to_email_forward(self):
        """P44: job_application now routes to email_forward, not slack directly."""
        result   = make_result(intent=Intent.JOB_APPLICATION, confidence=0.91)
        decision = route(result)
        assert decision.destination      == "email_forward"
        assert decision.channel_or_queue == "#hiring"

    def test_urgent_escalation_beats_human_review_priority(self):
        """Urgent escalation (priority 10) must win over human_review (priority 30)."""
        result   = make_result(
            intent=Intent.URGENT_ESCALATION,
            confidence=0.40,
            requires_human=True,
        )
        decision = route(result)
        assert decision.channel_or_queue == "#incidents"

    def test_routing_rules_sorted_at_module_load(self):
        """P37: ROUTING_RULES list is pre-sorted — verify invariant."""
        from src.router.rules import ROUTING_RULES
        priorities = [r.priority for r in ROUTING_RULES]
        assert priorities == sorted(priorities), "ROUTING_RULES must be sorted by priority"

    def test_always_returns_a_decision(self):
        """The catch-all fallback guarantees a result for any input."""
        for intent in Intent:
            result   = make_result(intent=intent)
            decision = route(result)
            assert isinstance(decision, RoutingDecision)
