"""
End-to-end feedback loop tests.
Requires both INTEGRATION_TEST_WEBHOOK_URL and INTEGRATION_TEST_FEEDBACK_URL.
"""

from __future__ import annotations

import os
import time
import uuid
import pytest
import httpx

WEBHOOK_URL  = os.environ.get("INTEGRATION_TEST_WEBHOOK_URL")
FEEDBACK_URL = os.environ.get("INTEGRATION_TEST_FEEDBACK_URL")

skip_if_no_urls = pytest.mark.skipif(
    not (WEBHOOK_URL and FEEDBACK_URL),
    reason="Integration test URLs not set",
)

SAMPLE_EMAIL = {
    "message_id": f"e2e-{uuid.uuid4()}",
    "from_address": "e2e@test.com",
    "from_name": "E2E Tester",
    "to_address": "inbox@company.com",
    "subject": "I need help with my order",
    "body_text": "My order hasn't arrived. Order number 99887. Please help.",
    "received_at": "2026-01-01T12:00:00Z",
    "source": "e2e_test",
}


@skip_if_no_urls
class TestFeedbackLoop:

    def test_classify_then_correct(self):
        """Submit an email, get a classification, submit a correction, verify it's stored."""
        # Step 1: Classify
        resp = httpx.post(WEBHOOK_URL, json=SAMPLE_EMAIL, timeout=30)
        assert resp.status_code == 200

        data = resp.json()
        record_id = data["record_id"]
        assert record_id

        # Step 2: Submit feedback correction
        time.sleep(1)  # brief pause for write propagation
        feedback_payload = {
            "record_id": record_id,
            "corrected_intent": "support_request",
            "reviewer": "e2e-test@example.com",
        }
        fb_resp = httpx.post(FEEDBACK_URL, json=feedback_payload, timeout=10)
        assert fb_resp.status_code == 200

        fb_data = fb_resp.json()
        assert fb_data["status"] == "ok"
        assert fb_data["corrected_intent"] == "support_request"

    def test_feedback_with_invalid_record_id(self):
        """Feedback for a non-existent record ID should return a 4xx."""
        payload = {
            "record_id": "does-not-exist-00000",
            "corrected_intent": "billing_question",
        }
        resp = httpx.post(FEEDBACK_URL, json=payload, timeout=10)
        assert resp.status_code in (400, 404, 422)

    def test_feedback_with_invalid_intent(self):
        """Feedback with an unknown intent class should return 422."""
        payload = {
            "record_id": "some-id",
            "corrected_intent": "not_a_real_intent",
        }
        resp = httpx.post(FEEDBACK_URL, json=payload, timeout=10)
        assert resp.status_code == 422
