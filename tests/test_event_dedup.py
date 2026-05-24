"""Tests for Slack event de-duplication and ack-before-download ordering."""

from unittest.mock import MagicMock, patch

import handlers
from nlp import AgentResult


def _patches():
    return (
        patch("handlers._record_metrics"),
        patch("handlers.run_agent", return_value=AgentResult(text="hi")),
        patch("handlers.check_access", return_value=None),
        patch("handlers.parse_admin_command", return_value=None),
    )


class TestEventDedup:
    def setup_method(self):
        handlers._seen_events.clear()

    @patch("handlers._extract_images", return_value=[])
    @patch("handlers._record_metrics")
    @patch("handlers.run_agent", return_value=AgentResult(text="hi"))
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_duplicate_event_processed_once(self, _adm, mock_run, _met, _img, _ex):
        say, client = MagicMock(), MagicMock()
        event = {"user": "U1", "channel": "C1", "ts": "1.23", "text": "hi",
                 "client_msg_id": "cmid-1"}
        handlers.handle_direct_message(event, say, client)
        handlers.handle_direct_message(event, say, client)  # Slack retry
        assert mock_run.call_count == 1

    @patch("handlers._extract_images", return_value=[])
    @patch("handlers._record_metrics")
    @patch("handlers.run_agent", return_value=AgentResult(text="hi"))
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_distinct_events_both_processed(self, _adm, mock_run, _met, _img, _ex):
        say, client = MagicMock(), MagicMock()
        e1 = {"user": "U1", "channel": "C1", "ts": "1.23", "text": "a", "client_msg_id": "m1"}
        e2 = {"user": "U1", "channel": "C1", "ts": "4.56", "text": "b", "client_msg_id": "m2"}
        handlers.handle_direct_message(e1, say, client)
        handlers.handle_direct_message(e2, say, client)
        assert mock_run.call_count == 2

    @patch("handlers._record_metrics")
    @patch("handlers.run_agent", return_value=AgentResult(text="hi"))
    @patch("handlers.check_access", return_value=None)
    @patch("handlers.parse_admin_command", return_value=None)
    def test_images_are_downloaded_after_ack_and_passed_through(self, _adm, _acc, mock_run, _met):
        say, client = MagicMock(), MagicMock()

        def extract_side_effect(event, cl):
            # The "Sniffing around…" ack must already have been posted.
            assert say.call_count >= 1
            return [{"media_type": "image/png", "data": "abc"}]

        with patch("handlers._extract_images", side_effect=extract_side_effect):
            event = {"user": "U1", "channel": "C1", "ts": "9.9", "text": "whose wine?",
                     "client_msg_id": "m9"}
            handlers.handle_direct_message(event, say, client)

        assert mock_run.call_args.kwargs["images"] == [{"media_type": "image/png", "data": "abc"}]
