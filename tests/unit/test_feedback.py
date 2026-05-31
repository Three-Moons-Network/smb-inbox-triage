"""Unit tests for the feedback handler."""
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pytest
from unittest.mock import patch

# Disable auth for most tests — re-enable in auth-specific tests
os.environ["FEEDBACK_AUTH_DISABLED"] = "true"
os.environ.setdefault("OBSERVABILITY_ENABLED", "false")

from src.feedback.handler import (
    FeedbackAuthError,
    FeedbackValidationError,
    handle_feedback_request,
    verify_signature,
)


def _make_signature(body: str | bytes, secret: str = "test-secret") -> str:
    """Helper: compute valid HMAC-SHA256 signature."""
    b = body if isinstance(body, bytes) else body.encode()
    digest = hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestFeedbackHandler:

    def test_valid_feedback_accepted(self):
        body = json.dumps({
            "record_id":        "test-uuid-1234",
            "corrected_intent": "support_request",
            "reviewer":         "jane@company.com",
        })
        with patch("src.feedback.handler.write_feedback") as mock_write:
            result = handle_feedback_request(body)

        mock_write.assert_called_once_with(
            record_id="test-uuid-1234",
            corrected_intent="support_request",
            reviewer="jane@company.com",
        )
        assert result["status"]           == "ok"
        assert result["corrected_intent"] == "support_request"

    def test_dict_input_accepted(self):
        body = {"record_id": "uuid-abc", "corrected_intent": "billing_question"}
        with patch("src.feedback.handler.write_feedback"):
            result = handle_feedback_request(body)
        assert result["reviewer"] == "anonymous"

    def test_missing_record_id_raises(self):
        with pytest.raises(FeedbackValidationError, match="record_id"):
            handle_feedback_request({"corrected_intent": "support_request"})

    def test_missing_corrected_intent_raises(self):
        with pytest.raises(FeedbackValidationError, match="corrected_intent"):
            handle_feedback_request({"record_id": "some-id"})

    def test_invalid_intent_raises(self):
        body = {"record_id": "some-id", "corrected_intent": "not_a_real_intent"}
        with pytest.raises(FeedbackValidationError, match="Invalid intent"):
            handle_feedback_request(body)

    def test_invalid_json_raises(self):
        with pytest.raises(FeedbackValidationError, match="Invalid JSON"):
            handle_feedback_request("not json")

    def test_all_valid_intents_accepted(self):
        from src.classifier.models import Intent
        for intent in Intent:
            body = {"record_id": "test-id", "corrected_intent": intent.value}
            with patch("src.feedback.handler.write_feedback"):
                result = handle_feedback_request(body)
            assert result["corrected_intent"] == intent.value

    # ── P15: non-string field guards ──────────────────────────────────────────

    def test_non_string_record_id_raises(self):
        """P15: record_id must be a string, not an int or list."""
        with pytest.raises(FeedbackValidationError, match="record_id"):
            handle_feedback_request({"record_id": 12345, "corrected_intent": "unknown"})

    def test_non_string_corrected_intent_raises(self):
        """P15: corrected_intent must be a string."""
        with pytest.raises(FeedbackValidationError, match="corrected_intent"):
            handle_feedback_request({"record_id": "some-id", "corrected_intent": ["support_request"]})

    def test_null_record_id_raises(self):
        with pytest.raises(FeedbackValidationError, match="record_id"):
            handle_feedback_request({"record_id": None, "corrected_intent": "unknown"})


class TestFeedbackAuth:
    """Tests for P1: HMAC-SHA256 webhook authentication."""

    def setup_method(self):
        """Re-enable auth for every test in this class."""
        os.environ["FEEDBACK_AUTH_DISABLED"] = "false"
        os.environ["FEEDBACK_WEBHOOK_SECRET"] = "test-secret-at-least-32-bytes-long!"

    def teardown_method(self):
        """Restore the global disable so other test classes continue without auth."""
        os.environ["FEEDBACK_AUTH_DISABLED"] = "true"
        os.environ.pop("FEEDBACK_WEBHOOK_SECRET", None)

    def test_valid_signature_accepted(self):
        body = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        sig  = _make_signature(body, "test-secret-at-least-32-bytes-long!")
        with patch("src.feedback.handler.write_feedback"):
            result = handle_feedback_request(body, signature_header=sig)
        assert result["status"] == "ok"

    def test_missing_signature_raises(self):
        body = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        with pytest.raises(FeedbackAuthError, match="Missing"):
            handle_feedback_request(body, signature_header=None)

    def test_wrong_signature_raises(self):
        body = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        with pytest.raises(FeedbackAuthError, match="verification failed"):
            handle_feedback_request(body, signature_header="sha256=deadbeef")

    def test_malformed_signature_header_raises(self):
        body = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        with pytest.raises(FeedbackAuthError, match="Malformed"):
            handle_feedback_request(body, signature_header="no-equals-sign")

    def test_missing_secret_raises(self):
        os.environ.pop("FEEDBACK_WEBHOOK_SECRET", None)
        body = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        with pytest.raises(FeedbackAuthError, match="not configured"):
            handle_feedback_request(body, signature_header="sha256=abc")

    def test_tampered_body_fails_signature(self):
        original = json.dumps({"record_id": "r1", "corrected_intent": "support_request"})
        sig      = _make_signature(original, "test-secret-at-least-32-bytes-long!")
        tampered = json.dumps({"record_id": "r1", "corrected_intent": "urgent_escalation"})
        with pytest.raises(FeedbackAuthError, match="verification failed"):
            handle_feedback_request(tampered, signature_header=sig)
