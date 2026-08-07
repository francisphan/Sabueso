"""Slack-facing Claude API error messages: actionable, no raw JSON walls."""

import anthropic
import httpx
import pytest

from nlp import _friendly_api_error

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status: int, body=None):
    resp = httpx.Response(status, request=_REQ, json=body or {})
    return cls("boom", response=resp, body=body)


class TestFriendlyApiError:
    def test_rate_limit(self):
        msg = _friendly_api_error(_status_error(anthropic.RateLimitError, 429), "abc123")
        assert "rate-limited" in msg
        assert "resend" in msg.lower()
        assert "abc123" in msg

    def test_overloaded_529(self):
        # The classic: Anthropic 529 overloaded_error used to reach Slack as a
        # raw JSON wall. Users should see what to do, not the payload.
        body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        msg = _friendly_api_error(_status_error(anthropic.InternalServerError, 529, body), "t1")
        assert "overloaded" in msg.lower()
        assert "minute" in msg
        assert "overloaded_error" not in msg  # no raw body leakage

    def test_connection_error(self):
        msg = _friendly_api_error(anthropic.APIConnectionError(request=_REQ), "t2")
        assert "reach" in msg
        assert "t2" in msg

    def test_auth_error_points_at_admin(self):
        msg = _friendly_api_error(_status_error(anthropic.AuthenticationError, 401), "t3")
        assert "admin" in msg
        assert "ANTHROPIC_API_KEY" in msg

    def test_bad_request_suggests_fresh_thread(self):
        msg = _friendly_api_error(_status_error(anthropic.BadRequestError, 400), "t4")
        assert "fresh" in msg

    @pytest.mark.parametrize("status", [429, 529, 401, 400])
    def test_never_leaks_raw_exception_repr(self, status):
        cls = {
            429: anthropic.RateLimitError,
            529: anthropic.InternalServerError,
            401: anthropic.AuthenticationError,
            400: anthropic.BadRequestError,
        }[status]
        exc = _status_error(cls, status, {"request_id": "req_secret123"})
        msg = _friendly_api_error(exc, "t5")
        assert "req_secret123" not in msg
        assert "boom" not in msg
