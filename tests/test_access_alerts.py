"""Tests for denied-access admin alerting."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import access_alerts
import handlers
import permissions


@pytest.fixture(autouse=True)
def fresh_state():
    access_alerts._records.clear()
    yield
    access_alerts._records.clear()


# ── Throttle logic ─────────────────────────────────────────────────────────

class TestRecordDeniedAttempt:
    def test_first_attempt_notifies(self):
        assert access_alerts.record_denied_attempt("U1", now=0.0) == 1

    def test_repeat_within_cooldown_is_suppressed(self):
        access_alerts.record_denied_attempt("U1", now=0.0)
        assert access_alerts.record_denied_attempt("U1", now=10.0) is None
        assert access_alerts.record_denied_attempt("U1", now=20.0) is None

    def test_after_cooldown_reports_suppressed_count(self):
        cooldown = access_alerts._COOLDOWN_SECONDS
        access_alerts.record_denied_attempt("U1", now=0.0)  # notice #1
        access_alerts.record_denied_attempt("U1", now=10.0)  # suppressed
        access_alerts.record_denied_attempt("U1", now=20.0)  # suppressed
        # Third attempt past the window: 2 suppressed + this one = 3.
        assert access_alerts.record_denied_attempt("U1", now=cooldown + 1) == 3

    def test_users_are_tracked_independently(self):
        access_alerts.record_denied_attempt("U1", now=0.0)
        assert access_alerts.record_denied_attempt("U2", now=1.0) == 1


# ── Recipients ─────────────────────────────────────────────────────────────

class TestAlertRecipients:
    def test_defaults_to_bootstrap_admin(self, monkeypatch):
        monkeypatch.delenv("ACCESS_ALERT_USER_IDS", raising=False)
        monkeypatch.setattr(permissions, "_DEFAULT_ADMIN", "UBOOT")
        assert access_alerts.alert_recipients() == ["UBOOT"]

    def test_env_override_parses_comma_list(self, monkeypatch):
        monkeypatch.setenv("ACCESS_ALERT_USER_IDS", "U1, U2 ,,U3")
        assert access_alerts.alert_recipients() == ["U1", "U2", "U3"]


# ── Notice text ────────────────────────────────────────────────────────────

class TestBuildNotice:
    def test_single_attempt_mentions_user_and_grant_command(self):
        text = access_alerts.build_notice("U9", 1, "Jane Doe")
        assert "<@U9>" in text
        assert "Jane Doe" in text
        assert "!access add <@U9> read_only" in text

    def test_repeat_attempts_include_count(self):
        text = access_alerts.build_notice("U9", 4)
        assert "4 times" in text


# ── Handler wiring ─────────────────────────────────────────────────────────

class TestHandlerNotifies:
    def setup_method(self):
        handlers._seen_events.clear()

    @patch("handlers._record_metrics")
    @patch("handlers.run_agent")
    @patch("handlers.check_access", return_value="not on my list")
    @patch("handlers.parse_admin_command", return_value=None)
    def test_denied_user_triggers_admin_dm(self, _adm, _acc, mock_run, _met, monkeypatch):
        monkeypatch.setenv("ACCESS_ALERT_USER_IDS", "UADMIN")
        say, client = MagicMock(), MagicMock()
        client.users_info.return_value = {
            "user": {"profile": {"display_name": "Intruder"}}
        }
        event = {"user": "UNOBODY", "channel": "D1", "ts": "1.0", "text": "hi",
                 "client_msg_id": "d1"}
        handlers.handle_direct_message(event, say, client)

        mock_run.assert_not_called()
        say.assert_called_once()
        assert "not on my list" in say.call_args.kwargs["text"]
        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "UADMIN"
        assert "<@UNOBODY>" in kwargs["text"]
        assert "Intruder" in kwargs["text"]

    @patch("handlers._record_metrics")
    @patch("handlers.run_agent")
    @patch("handlers.check_access", return_value="not on my list")
    @patch("handlers.parse_admin_command", return_value=None)
    def test_repeat_denial_is_throttled(self, _adm, _acc, mock_run, _met, monkeypatch):
        monkeypatch.setenv("ACCESS_ALERT_USER_IDS", "UADMIN")
        say, client = MagicMock(), MagicMock()
        client.users_info.return_value = {"user": {"profile": {}}}
        for i in range(3):
            event = {"user": "UNOBODY", "channel": "D1", "ts": f"{i}.0",
                     "text": "hi", "client_msg_id": f"d{i}"}
            handlers.handle_direct_message(event, say, client)

        assert say.call_count == 3  # every attempt still gets the denial reply
        assert client.chat_postMessage.call_count == 1  # but only one admin DM

    @patch("handlers._record_metrics")
    @patch("handlers.run_agent")
    @patch("handlers.check_access", return_value="not on my list")
    @patch("handlers.parse_admin_command", return_value=None)
    def test_slack_failure_never_breaks_denial_reply(self, _adm, _acc, mock_run, _met,
                                                     monkeypatch):
        monkeypatch.setenv("ACCESS_ALERT_USER_IDS", "UADMIN")
        say, client = MagicMock(), MagicMock()
        client.users_info.side_effect = RuntimeError("slack down")
        client.chat_postMessage.side_effect = RuntimeError("slack down")
        event = {"user": "UNOBODY", "channel": "D1", "ts": "9.0", "text": "hi",
                 "client_msg_id": "d9"}
        handlers.handle_direct_message(event, say, client)
        say.assert_called_once()
