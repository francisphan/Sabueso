"""Tests for sf_identity — Slack user → Salesforce User ID resolution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import sf_identity
from sf_identity import clear_cache_entry, get_sf_user_id_for_slack

SLACK_USER_ID = "USLACK1"
SF_USER_ID = "005xx0000012345"
EMAIL = "alice@example.com"


def _make_slack_client(email: str | None) -> MagicMock:
    """Return a fake Slack client whose users_info returns the given email."""
    client = MagicMock()
    profile: dict = {}
    if email is not None:
        profile["email"] = email
    client.users_info.return_value = {"user": {"profile": profile}}
    return client


def _soql_one_match(sf_user_id: str) -> dict:
    return {"records": [{"Id": sf_user_id}]}


def _soql_no_match() -> dict:
    return {"records": []}


def _soql_multi_match() -> dict:
    return {"records": [{"Id": "005aaa"}, {"Id": "005bbb"}]}


@pytest.fixture(autouse=True)
def reset_sf_identity(monkeypatch, tmp_path):
    """Redirect _CACHE_PATH to tmp dir and wipe the in-memory cache."""
    cache_file = tmp_path / "sf_user_map.json"
    monkeypatch.setattr(sf_identity, "_CACHE_PATH", cache_file)
    sf_identity._cache = None
    yield
    sf_identity._cache = None


# ── Override hit ───────────────────────────────────────────────────────────

class TestOverrideHit:
    def test_override_short_circuits_all_lookups(self, monkeypatch):
        """ACL has sf_user_id — no Slack call, no SOQL call."""
        monkeypatch.setattr(
            sf_identity.permissions,
            "get_sf_user_override",
            lambda uid: SF_USER_ID,
        )

        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result == SF_USER_ID
        slack_client.users_info.assert_not_called()
        call_tool_mock.assert_not_called()


# ── Cache hit ──────────────────────────────────────────────────────────────

class TestCacheHit:
    def test_cache_hit_bypasses_slack_and_soql(self, monkeypatch, tmp_path):
        """Cached mapping returns immediately — no Slack or SOQL call."""
        # Seed the file cache
        cache_file = tmp_path / "sf_user_map.json"
        cache_file.write_text(json.dumps({SLACK_USER_ID: SF_USER_ID}))
        monkeypatch.setattr(sf_identity, "_CACHE_PATH", cache_file)
        sf_identity._cache = None  # force re-read from file

        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result == SF_USER_ID
        slack_client.users_info.assert_not_called()
        call_tool_mock.assert_not_called()


# ── Full lookup flow ────────────────────────────────────────────────────────

class TestFullFlow:
    def test_full_flow_caches_result(self, monkeypatch, tmp_path):
        """No override, no cache → Slack email → SOQL → caches and returns."""
        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock(return_value=_soql_one_match(SF_USER_ID))
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result == SF_USER_ID
        slack_client.users_info.assert_called_once_with(user=SLACK_USER_ID)
        call_tool_mock.assert_called_once()

        # Cache file should be persisted
        cache_file = sf_identity._CACHE_PATH
        cached = json.loads(cache_file.read_text())
        assert cached.get(SLACK_USER_ID) == SF_USER_ID

    def test_no_email_returns_none(self, monkeypatch):
        """Slack profile has no email → returns None."""
        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(None)
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result is None
        call_tool_mock.assert_not_called()

    def test_soql_zero_matches_returns_none(self, monkeypatch):
        """SOQL returns no users → returns None."""
        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock(return_value=_soql_no_match())
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result is None

    def test_soql_multiple_matches_returns_none(self, monkeypatch):
        """SOQL returns >1 user — refuses to guess, returns None."""
        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock(return_value=_soql_multi_match())
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        assert result is None

    def test_soql_query_escapes_single_quote_in_email(self, monkeypatch):
        """An email containing ' must be escaped; the SOQL query must not break."""
        evil_email = "o'reilly@example.com"

        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(evil_email)
        call_tool_mock = MagicMock(return_value=_soql_one_match(SF_USER_ID))
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)

        call_tool_mock.assert_called_once()
        # Extract the query string from the call
        call_args = call_tool_mock.call_args
        query_str = call_args[0][1]["query_str"]

        # The email must appear escaped in the query
        assert "o\\'reilly@example.com" in query_str
        # There must be no unescaped single quote that could break the SOQL
        # (the value portion between the outer quotes should be escaped)
        assert "o'reilly" not in query_str


# ── clear_cache_entry ──────────────────────────────────────────────────────

class TestClearCacheEntry:
    def test_clear_removes_cached_entry(self, monkeypatch, tmp_path):
        """clear_cache_entry removes the user from the in-memory and file cache."""
        monkeypatch.setattr(
            sf_identity.permissions, "get_sf_user_override", lambda uid: None
        )
        slack_client = _make_slack_client(EMAIL)
        call_tool_mock = MagicMock(return_value=_soql_one_match(SF_USER_ID))
        monkeypatch.setattr(sf_identity, "call_tool", call_tool_mock)

        # Populate cache
        result = get_sf_user_id_for_slack(SLACK_USER_ID, slack_client)
        assert result == SF_USER_ID

        # Clear it
        clear_cache_entry(SLACK_USER_ID)

        # In-memory cache should no longer have the entry
        assert sf_identity._cache is not None
        assert SLACK_USER_ID not in sf_identity._cache

        # File cache should not have the entry either
        cache_file = sf_identity._CACHE_PATH
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            assert SLACK_USER_ID not in cached

    def test_clear_nonexistent_entry_is_safe(self):
        """Clearing a user that was never cached should not raise."""
        clear_cache_entry("UNKNOWN_USER")  # should not raise
