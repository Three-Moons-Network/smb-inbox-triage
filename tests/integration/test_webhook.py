"""
Integration tests — verify end-to-end pipeline against a deployed webhook.
Requires a live cloud deployment. Skipped in CI unless INTEGRATION_TEST_WEBHOOK_URL is set.

Usage:
    INTEGRATION_TEST_WEBHOOK_URL=https://... pytest tests/integration/
"""

from __future__ import annotations

import json
import os
import pytest
import httpx

WEBHOOK_URL = os.environ.get("INTEGRATION_TEST_WEBHOOK_URL")
skip_if_no_url = pytest.mark.skipif(
    not WEBHOOK_URL,
    reason="INTEGRATION_TEST_WEBHOOK_URL not set — skipping integration tests",
)

SAMPLE_GMAIL_PAYLOAD = {
    "message": {
        "data": "eyJlbWFpbEFkZHJlc3MiOiAidGVzdEBleGFtcGxlLmNvbSIsICJoaXN0b3J5SWQiOiAxMjM0NX0=",
        "messageId": "test-pubsub-msg-001",
        "publishTime": "2026-01-01T12:00:00Z",
    },
    "subscription": "projects/test-project/subscriptions/inbox-triage-dev-gmail-inbound-sub",
}

SAMPLE_DIRECT_PAYLOAD = {
    "message_id": "int-test-001",
    "from_address": "prospect@example.com",
    "from_name": "Test Prospect",
    "to_address": "inbox@company.com",
    "subject": "Interested in your enterprise plan",
    "body_text": "Hi, we'd love to learn more about enterprise pricing and schedule a demo.",
    "received_at": "2026-01-01T12:00:00Z",
    "source": "integration_test",
}


@skip_if_no_url
class TestWebhookIntegration:

    def test_webhook_returns_200(self):
        resp = httpx.post(WEBHOOK_URL, json=SAMPLE_DIRECT_PAYLOAD, timeout=30)
        assert resp.status_code == 200

    def test_webhook_returns_json(self):
        resp = httpx.post(WEBHOOK_URL, json=SAMPLE_DIRECT_PAYLOAD, timeout=30)
        data = resp.json()
        assert "record_id" in data
        assert "intent" in data

    def test_webhook_classifies_sales_inquiry(self):
        resp = httpx.post(WEBHOOK_URL, json=SAMPLE_DIRECT_PAYLOAD, timeout=30)
        data = resp.json()
        # Should be sales_inquiry with reasonable confidence
        assert data["intent"] == "sales_inquiry"
        assert data["confidence"] >= 0.75

    def test_webhook_rejects_missing_fields(self):
        resp = httpx.post(WEBHOOK_URL, json={"subject": "only subject"}, timeout=10)
        assert resp.status_code == 422

    def test_webhook_handles_empty_body(self):
        payload = {**SAMPLE_DIRECT_PAYLOAD, "body_text": ""}
        resp = httpx.post(WEBHOOK_URL, json=payload, timeout=30)
        # Short body should route to human review, not crash
        assert resp.status_code in (200, 422)
