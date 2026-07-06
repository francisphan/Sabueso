"""Tests for per-turn tracing context (correlation ID + end user) and its
propagation to MCP headers."""

from unittest.mock import MagicMock, patch

import handlers
import mcp_client
import tracing
from nlp import AgentResult


class TestTracing:
    def teardown_method(self):
        tracing.set_correlation_id(None)
        tracing.set_end_user(None)

    def test_new_turn_id_is_unique_hex(self):
        a, b = tracing.new_turn_id(), tracing.new_turn_id()
        assert a != b
        assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)

    def test_set_and_get(self):
        tracing.set_correlation_id("turn-1")
        assert tracing.get_correlation_id() == "turn-1"

    def test_default_is_none(self):
        tracing.set_correlation_id(None)
        assert tracing.get_correlation_id() is None

    def test_set_and_get_end_user(self):
        tracing.set_end_user("U123")
        assert tracing.get_end_user() == "U123"

    def test_end_user_default_is_none(self):
        tracing.set_end_user(None)
        assert tracing.get_end_user() is None


class TestHeaderPropagation:
    def teardown_method(self):
        tracing.set_correlation_id(None)
        tracing.set_end_user(None)

    def test_header_present_when_correlation_set(self):
        tracing.set_correlation_id("turn-xyz")
        assert mcp_client._headers()["X-Correlation-ID"] == "turn-xyz"

    def test_header_absent_when_no_correlation(self):
        tracing.set_correlation_id(None)
        assert "X-Correlation-ID" not in mcp_client._headers()

    def test_end_user_header_present_when_set(self):
        tracing.set_end_user("U999")
        assert mcp_client._headers()["X-End-User"] == "U999"

    def test_end_user_header_absent_when_not_set(self):
        tracing.set_end_user(None)
        assert "X-End-User" not in mcp_client._headers()


class TestEndUserPerTurn:
    """The Slack user is bound for the turn and cleared afterward, so a pooled
    worker thread never carries one turn's user into the next."""

    def setup_method(self):
        handlers._seen_events.clear()
        tracing.set_end_user(None)

    def teardown_method(self):
        tracing.set_end_user(None)

    @patch("handlers._extract_images", return_value=[])
    @patch("handlers._record_metrics")
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_end_user_set_during_turn_and_cleared_after(self, _adm, _acc, _met, _img):
        seen: list[str | None] = []

        def capture(**kwargs):
            # get_end_user() is what mcp_client._headers() would read on a tool call.
            seen.append(tracing.get_end_user())
            return AgentResult(text="hi")

        say, client = MagicMock(), MagicMock()
        event = {"user": "U123", "channel": "C1", "ts": "1.1", "text": "hi",
                 "client_msg_id": "m1"}
        with patch("handlers.run_agent", side_effect=capture):
            handlers.handle_direct_message(event, say, client)

        assert seen == ["U123"]                # bound while the agent ran
        assert tracing.get_end_user() is None  # cleared on the way out

    @patch("handlers._extract_images", return_value=[])
    @patch("handlers._record_metrics")
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_end_user_cleared_even_when_turn_errors(self, _adm, _acc, _met, _img):
        say, client = MagicMock(), MagicMock()
        event = {"user": "U9", "channel": "C1", "ts": "3.3", "text": "boom",
                 "client_msg_id": "m3"}
        with patch("handlers.run_agent", side_effect=RuntimeError("boom")):
            handlers.handle_direct_message(event, say, client)

        assert tracing.get_end_user() is None

    @patch("handlers._extract_images", return_value=[])
    @patch("handlers._record_metrics")
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_no_leakage_between_consecutive_turns(self, _adm, _acc, _met, _img):
        seen: list[str | None] = []

        def capture(**kwargs):
            seen.append(tracing.get_end_user())
            return AgentResult(text="hi")

        say, client = MagicMock(), MagicMock()
        with patch("handlers.run_agent", side_effect=capture):
            handlers.handle_direct_message(
                {"user": "U1", "channel": "C1", "ts": "1.1", "text": "a", "client_msg_id": "m1"},
                say, client,
            )
            handlers.handle_direct_message(
                {"user": "U2", "channel": "C1", "ts": "2.2", "text": "b", "client_msg_id": "m2"},
                say, client,
            )

        assert seen == ["U1", "U2"]            # each turn saw only its own user
        assert tracing.get_end_user() is None
