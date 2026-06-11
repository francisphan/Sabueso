"""Tests for sf_intents — helpers + create_opportunity_for_person + log_touch.

Mocking strategy:
  - Patch sf_intents.call_tool  (NOT mcp_client.call_tool — sf_intents imports it directly)
  - Patch sf_intents.sf_identity.get_sf_user_id_for_slack
  - Patch sf_intents.audit.record_write
  - Monkeypatch sf_intents._SF_INSTANCE_URL for predictable opp URLs
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import sf_intents
from sf_intents import (
    _is_sf_id,
    _sanitize_sosl,
    end_of_current_quarter,
    parse_when,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

SLACK_USER_ID = "USLACK1"
SF_USER_ID = "005xx0000012345AAA"   # 18 chars
ACCOUNT_ID = "001XXXXXXXXXX0001A"   # 18 chars
OPP_ID = "006XXXXXXXXXX0001A"       # 18 chars
TASK_ID_1 = "00TXXXXXXXXX0001A"     # 18 chars
TASK_ID_2 = "00TXXXXXXXXX0002B"     # 18 chars
INSTANCE_URL = "https://example.salesforce.com"

VALID_PRODUCT = "TVOM Vineyards"
VALID_SUBJECT = "Call"


# ---------------------------------------------------------------------------
# Shared helpers for building fake SF responses
# ---------------------------------------------------------------------------

def _make_slack_client() -> MagicMock:
    return MagicMock()


def _person_account_rec(account_id=ACCOUNT_ID, name="Alice Smith", email="alice@example.com"):
    return {
        "attributes": {"type": "Account"},
        "Id": account_id,
        "Name": name,
        "PersonEmail": email,
        "IsPersonAccount": True,
    }


def _sosl_one_person(account_id=ACCOUNT_ID):
    return {"searchRecords": [_person_account_rec(account_id=account_id)]}


def _sosl_multi_person():
    return {
        "searchRecords": [
            _person_account_rec(account_id="001AAAAAAAAAAAAAAAA", name="Alice Smith"),
            _person_account_rec(account_id="001BBBBBBBBBBBBBBB1", name="Alice Brown"),
        ]
    }


def _sosl_empty():
    return {"searchRecords": []}


def _soql_one_opp(opp_id=OPP_ID):
    return {
        "records": [
            {
                "Id": opp_id,
                "Name": "Alice Smith - TVOM Vineyards",
                "StageName": "Deep Discovery",
                "IsClosed": False,
                "Account": {"Name": "Alice Smith"},
            }
        ]
    }


def _soql_multi_opp():
    return {
        "records": [
            {
                "Id": "006AAAAAAAAAAAAAAAA",
                "Name": "Opp A",
                "StageName": "Discovery",
                "IsClosed": False,
                "Account": {"Name": "Alice"},
            },
            {
                "Id": "006BBBBBBBBBBBBBBB1",
                "Name": "Opp B",
                "StageName": "Discovery",
                "IsClosed": False,
                "Account": {"Name": "Alice"},
            },
        ]
    }


def _soql_empty():
    return {"records": []}


# ---------------------------------------------------------------------------
# Autouse fixture: predictable instance URL for all tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_instance_url(monkeypatch):
    monkeypatch.setattr(sf_intents, "_SF_INSTANCE_URL", INSTANCE_URL)


# ---------------------------------------------------------------------------
# Convenience fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_audit(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(sf_intents.audit, "record_write", mock)
    return mock


@pytest.fixture
def mock_sf_identity(monkeypatch):
    monkeypatch.setattr(
        sf_intents.sf_identity,
        "get_sf_user_id_for_slack",
        lambda uid, client: SF_USER_ID,
    )


@pytest.fixture
def mock_sf_identity_none(monkeypatch):
    monkeypatch.setattr(
        sf_intents.sf_identity,
        "get_sf_user_id_for_slack",
        lambda uid, client: None,
    )


# ===========================================================================
# Helper: end_of_current_quarter
# ===========================================================================

class TestEndOfCurrentQuarter:
    """Pass a specific date directly — no date.today() patching needed."""

    def test_q1_middle_returns_mar31(self):
        assert end_of_current_quarter(date(2024, 1, 15)) == date(2024, 3, 31)

    def test_q2_middle_returns_jun30(self):
        assert end_of_current_quarter(date(2024, 5, 1)) == date(2024, 6, 30)

    def test_q3_middle_returns_sep30(self):
        assert end_of_current_quarter(date(2024, 8, 1)) == date(2024, 9, 30)

    def test_q4_middle_returns_dec31(self):
        assert end_of_current_quarter(date(2024, 11, 1)) == date(2024, 12, 31)

    def test_today_is_quarter_end_rolls_forward(self):
        # delta = 0 days → (0 < 5) → rolls to next quarter
        assert end_of_current_quarter(date(2024, 3, 31)) == date(2024, 6, 30)

    def test_one_day_before_quarter_end_rolls(self):
        # delta = 1 → (1 < 5) → rolls
        assert end_of_current_quarter(date(2024, 3, 30)) == date(2024, 6, 30)

    def test_four_days_before_quarter_end_rolls(self):
        # delta = 4 → (4 < 5) → rolls
        assert end_of_current_quarter(date(2024, 3, 27)) == date(2024, 6, 30)

    def test_exactly_five_days_before_does_not_roll(self):
        # delta = 5 → (5 < 5) is False → stays at current quarter end
        # NOTE: the docstring says "within 5 days" but the implementation uses < 5
        # (strictly), so exactly 5 days before does NOT roll. See bug report.
        assert end_of_current_quarter(date(2024, 3, 26)) == date(2024, 3, 31)

    def test_six_days_before_does_not_roll(self):
        # delta = 6 → safely in current quarter
        assert end_of_current_quarter(date(2024, 3, 25)) == date(2024, 3, 31)

    def test_q4_end_rolls_to_next_year_q1(self):
        # Dec 31 → delta = 0 < 5 → rolls to Mar 31 of next year
        assert end_of_current_quarter(date(2024, 12, 31)) == date(2025, 3, 31)

    def test_q4_near_end_rolls_to_next_year_q1(self):
        # Dec 29 → delta = 2 < 5 → rolls to Mar 31 next year
        assert end_of_current_quarter(date(2024, 12, 29)) == date(2025, 3, 31)

    def test_q4_five_days_before_does_not_roll(self):
        # Dec 26 → delta = 5 → NOT < 5 → stays at Dec 31
        assert end_of_current_quarter(date(2024, 12, 26)) == date(2024, 12, 31)

    def test_q2_rolls_to_q3(self):
        # Jun 28 → delta = 2 < 5 → rolls from Jun 30 to Sep 30
        assert end_of_current_quarter(date(2024, 6, 28)) == date(2024, 9, 30)

    def test_q3_rolls_to_q4(self):
        # Sep 27 → delta = 3 < 5 → rolls from Sep 30 to Dec 31
        assert end_of_current_quarter(date(2024, 9, 27)) == date(2024, 12, 31)


# ===========================================================================
# Helper: parse_when
# ===========================================================================

class TestParseWhen:
    def test_empty_string_returns_today(self):
        assert parse_when("") == date.today()

    def test_today_string_returns_today(self):
        assert parse_when("today") == date.today()

    def test_today_string_case_insensitive(self):
        assert parse_when("TODAY") == date.today()
        assert parse_when("Today") == date.today()

    def test_valid_iso_date(self):
        assert parse_when("2025-06-15") == date(2025, 6, 15)

    def test_valid_iso_date_different_year(self):
        assert parse_when("2024-01-01") == date(2024, 1, 1)

    def test_invalid_string_falls_back_to_today(self):
        assert parse_when("not-a-date") == date.today()

    def test_random_garbage_falls_back_to_today(self):
        assert parse_when("!!??") == date.today()

    def test_whitespace_only_falls_back_to_today(self):
        # "  " → strip → "" → fromisoformat("") → ValueError → fallback
        assert parse_when("  ") == date.today()


# ===========================================================================
# Helper: _sanitize_sosl
# ===========================================================================

class TestSanitizeSosl:
    def test_empty_returns_empty(self):
        assert _sanitize_sosl("") == ""

    def test_safe_chars_pass_through(self):
        assert _sanitize_sosl("John Doe") == "John Doe"

    def test_alphanumeric_and_spaces_pass_through(self):
        assert _sanitize_sosl("Alice Smith 123") == "Alice Smith 123"

    def test_escapes_backslash(self):
        # Input: a\b (3 chars) → a\\b (4 chars)
        assert _sanitize_sosl("a\\b") == "a\\\\b"

    def test_escapes_open_brace(self):
        assert _sanitize_sosl("a{b") == "a\\{b"

    def test_escapes_close_brace(self):
        assert _sanitize_sosl("a}b") == "a\\}b"

    def test_escapes_braces(self):
        assert _sanitize_sosl("{hello}") == "\\{hello\\}"

    def test_escapes_single_quote(self):
        assert _sanitize_sosl("it's") == "it\\'s"

    def test_escapes_double_quote(self):
        assert _sanitize_sosl('say "hi"') == 'say \\"hi\\"'

    def test_escapes_multiple_special_chars(self):
        result = _sanitize_sosl('{hello "world"}')
        assert result == '\\{hello \\"world\\"\\}'

    def test_backslash_escaped_before_braces(self):
        # Input chars: \, {   (2 chars)
        # Step 1: \ → \\   result: \\{  (3 chars)
        # Step 2: { → \{   result: \\\{ (4 chars)
        result = _sanitize_sosl("\\{")
        # 4-char string: \, \, \, {
        assert result == "\\\\\\{"

    def test_empty_string_is_safe(self):
        # Sanity: double-call is idempotent only if no backslashes are introduced
        result = _sanitize_sosl("")
        assert result == ""


# ===========================================================================
# Helper: _is_sf_id
# ===========================================================================

class TestIsSfId:
    # Valid: 15-char alphanumeric
    def test_15_char_alphanumeric_true(self):
        s = "A" * 15  # 15 chars, all alphanum
        assert _is_sf_id(s) is True

    def test_15_char_starting_006_true(self):
        s = "006" + "A" * 12  # 15 chars
        assert _is_sf_id(s) is True

    # Valid: 18-char alphanumeric
    def test_18_char_alphanumeric_true(self):
        s = "A" * 18
        assert _is_sf_id(s) is True

    def test_18_char_starting_006_true(self):
        s = "006" + "A" * 15  # 18 chars
        assert _is_sf_id(s) is True

    # Invalid lengths
    def test_14_chars_false(self):
        assert _is_sf_id("A" * 14) is False

    def test_16_chars_false(self):
        assert _is_sf_id("A" * 16) is False

    def test_17_chars_false(self):
        assert _is_sf_id("A" * 17) is False

    def test_19_chars_false(self):
        assert _is_sf_id("A" * 19) is False

    def test_too_short_false(self):
        assert _is_sf_id("006") is False

    # Invalid characters
    def test_hyphen_in_string_false(self):
        assert _is_sf_id("006-AAAAAAAAAAAAA") is False  # contains hyphen

    def test_space_in_string_false(self):
        assert _is_sf_id("006 AAAAAAAAAAAAAA") is False

    def test_empty_string_false(self):
        assert _is_sf_id("") is False

    def test_only_digits_15_true(self):
        # Digits are alphanumeric
        assert _is_sf_id("0" * 15) is True

    def test_mixed_case_18_true(self):
        assert _is_sf_id("006aAbBcCdDeEfF123") is True  # 18 chars


# ===========================================================================
# create_opportunity_for_person
# ===========================================================================

class TestCreateOpportunityForPerson:
    # -----------------------------------------------------------------------
    # Happy path
    # -----------------------------------------------------------------------

    def test_happy_path_one_account_two_touches(self, monkeypatch, mock_audit, mock_sf_identity):
        """SOSL → one person → Opp created → 2 Tasks created → status=ok."""
        task_ids = iter([TASK_ID_1, TASK_ID_2])

        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                return {"id": next(task_ids), "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[
                {"subject": "Call", "notes": "First call", "when": "2025-06-01"},
                {"subject": "Conversation", "notes": "Follow-up", "when": "2025-06-02"},
            ],
        )

        assert result["status"] == "ok"
        assert result["opp_id"] == OPP_ID
        assert result["opp_url"] == f"{INSTANCE_URL}/lightning/r/Opportunity/{OPP_ID}/view"
        assert len(result["touches"]) == 2
        assert all(t["ok"] for t in result["touches"])
        assert result["touches"][0]["task_id"] == TASK_ID_1
        assert result["touches"][1]["task_id"] == TASK_ID_2
        mock_audit.assert_called_once()

    # -----------------------------------------------------------------------
    # Validation failures (happen before any SF calls)
    # -----------------------------------------------------------------------

    def test_invalid_product(self, monkeypatch):
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product="Fake Wine Package",
        )

        assert result["status"] == "invalid_product"
        assert "expected" in result
        assert isinstance(result["expected"], list)
        call_tool_mock.assert_not_called()

    def test_invalid_subject(self, monkeypatch):
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[{"subject": "Skywriting", "notes": "", "when": "today"}],
        )

        assert result["status"] == "invalid_subject"
        assert result["subject"] == "Skywriting"
        call_tool_mock.assert_not_called()

    def test_invalid_subject_first_touch_only(self, monkeypatch):
        """First touch invalid subject is caught before any SF call."""
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[
                {"subject": "Call", "notes": "", "when": "today"},
                {"subject": "BadSubject", "notes": "", "when": "today"},
            ],
        )

        assert result["status"] == "invalid_subject"
        assert result["subject"] == "BadSubject"
        call_tool_mock.assert_not_called()

    # -----------------------------------------------------------------------
    # Person resolution failures
    # -----------------------------------------------------------------------

    def test_needs_person_details_when_sosl_empty(self, monkeypatch, mock_sf_identity):
        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(return_value=_sosl_empty()))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="NoOne",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "needs_person_details"

    def test_ambiguous_person_returns_candidates(self, monkeypatch, mock_sf_identity):
        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(return_value=_sosl_multi_person()))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "ambiguous_person"
        assert 1 <= len(result["candidates"]) <= 5
        # Each candidate has required keys
        for c in result["candidates"]:
            assert "name" in c
            assert "email" in c
            assert "account_id" in c
            assert "source" in c

    # -----------------------------------------------------------------------
    # SF identity failure
    # -----------------------------------------------------------------------

    def test_no_sf_identity_sends_empty_owner_id(self, monkeypatch, mock_sf_identity_none):
        """Missing Slack→SF user mapping is non-fatal — the composite is called
        with an empty owner_id so SF defaults to the API-connection's
        authenticated user."""
        calls = []

        def side_effect(tool, params):
            calls.append((tool, params))
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "ok"
        opp_create_call = next(
            (params for tool, params in calls if tool == "create_opportunity"),
            None,
        )
        assert opp_create_call is not None
        assert opp_create_call["owner_id"] == ""

    # -----------------------------------------------------------------------
    # Opportunity creation failures
    # -----------------------------------------------------------------------

    def test_create_failed_when_opp_returns_no_id(self, monkeypatch, mock_sf_identity):
        """Composite reports status=ok but no opportunity_id → create_failed."""
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok"}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "create_failed"

    def test_create_failed_when_opp_status_not_ok(self, monkeypatch, mock_sf_identity):
        """Composite reports a non-ok status (one NOT in the pass-through set
        needs_account/ambiguous_account/account_not_found) → create_failed."""
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "error", "error": "SOME_ERROR"}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "create_failed"
        assert "SOME_ERROR" in result["error"]

    def test_create_failed_when_opp_raises(self, monkeypatch, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            raise RuntimeError("Network error")

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["status"] == "create_failed"
        assert "Network error" in result["error"]

    # -----------------------------------------------------------------------
    # Partial failure: opp succeeds, second Task raises
    # -----------------------------------------------------------------------

    def test_partial_failure_second_task_raises_status_ok(
        self, monkeypatch, mock_audit, mock_sf_identity
    ):
        """Opp succeeds; second Task raises → status=ok, second touch ok=False, NO rollback."""
        task_call_count = 0

        def side_effect(tool, params):
            nonlocal task_call_count
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                task_call_count += 1
                if task_call_count == 1:
                    return {"id": TASK_ID_1, "success": True}
                raise RuntimeError("Task creation failed")
            return {}

        call_tool_mock = MagicMock(side_effect=side_effect)
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[
                {"subject": "Call", "notes": "ok", "when": "today"},
                {"subject": "Conversation", "notes": "fail", "when": "today"},
            ],
        )

        # Result is still ok
        assert result["status"] == "ok"
        assert result["opp_id"] == OPP_ID

        # Touch results reflect partial success
        assert len(result["touches"]) == 2
        assert result["touches"][0]["ok"] is True
        assert result["touches"][0]["task_id"] == TASK_ID_1
        assert result["touches"][1]["ok"] is False
        assert "error" in result["touches"][1]

        # No rollback: no sf_delete_record call was made
        all_tool_names = [c[0][0] for c in call_tool_mock.call_args_list]
        assert "sf_delete_record" not in all_tool_names

        # Audit was still called and reflects both touches
        mock_audit.assert_called_once()
        record = mock_audit.call_args[0][0]
        assert record["opp_id"] == OPP_ID
        touches_in_audit = record["touches"]
        assert touches_in_audit[0]["ok"] is True
        assert touches_in_audit[1]["ok"] is False

    # -----------------------------------------------------------------------
    # Payload assertions: description marker, owner injection, composite params
    # -----------------------------------------------------------------------

    def test_description_contains_created_marker(self, monkeypatch, mock_audit, mock_sf_identity):
        """description passed to create_opportunity contains
        'Created via Sabueso by <@USER> on DATE'."""
        captured = {}

        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                captured["params"] = params
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            notes="Meeting at the vineyard",
        )

        desc = captured["params"].get("description", "")
        today_str = date.today().isoformat()
        assert f"Created via Sabueso by <@{SLACK_USER_ID}> on {today_str}" in desc
        # Notes should also appear
        assert "Meeting at the vineyard" in desc

    def test_owner_id_equals_sf_identity_not_llm(self, monkeypatch, mock_audit, mock_sf_identity):
        """owner_id passed to create_opportunity must equal the resolved SF user ID."""
        captured = {}

        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                captured["params"] = params
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert captured["params"]["owner_id"] == SF_USER_ID

    def test_close_date_owned_by_composite_not_client(
        self, monkeypatch, mock_audit, mock_sf_identity
    ):
        """CloseDate is now owned by Agent B's create_opportunity composite, so
        the client can't assert its value (the old end-of-quarter check moved
        server-side). Instead, assert what the client IS responsible for: it
        sends the resolved account_id and product, and does NOT try to set a
        CloseDate itself."""
        captured = {}

        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                captured["params"] = params
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert captured["params"]["account_id"] == ACCOUNT_ID
        assert captured["params"]["product"] == VALID_PRODUCT
        assert "CloseDate" not in captured["params"]
        assert "close_date" not in captured["params"]

    def test_task_owner_id_matches_sf_identity(self, monkeypatch, mock_audit, mock_sf_identity):
        """OwnerId on each Task also equals SF user ID."""
        task_owners = []

        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                task_owners.append(params.get("data", {}).get("OwnerId"))
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[{"subject": "Call", "notes": "", "when": "today"}],
        )

        assert task_owners == [SF_USER_ID]

    def test_opp_url_in_result(self, monkeypatch, mock_audit, mock_sf_identity):
        """Result opp_url points to the Lightning record page."""
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
        )

        assert result["opp_url"] == f"{INSTANCE_URL}/lightning/r/Opportunity/{OPP_ID}/view"

    def test_audit_keys(self, monkeypatch, mock_audit, mock_sf_identity):
        """audit.record_write is called with all required keys."""
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "create_opportunity":
                return {"status": "ok", "opportunity_id": OPP_ID}
            if tool == "sf_create_record" and params.get("object_name") == "Task":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.create_opportunity_for_person(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            product=VALID_PRODUCT,
            touches=[{"subject": "Call", "notes": "", "when": "today"}],
        )

        mock_audit.assert_called_once()
        record = mock_audit.call_args[0][0]
        for key in ("tool", "slack_user", "sf_user", "opp_id", "product", "person_account_id",
                    "person_name", "touches", "lead_source"):
            assert key in record, f"Missing audit key: {key}"
        assert record["slack_user"] == SLACK_USER_ID
        assert record["sf_user"] == SF_USER_ID
        assert record["opp_id"] == OPP_ID


# ===========================================================================
# log_touch
# ===========================================================================

class TestLogTouch:
    # -----------------------------------------------------------------------
    # Happy paths
    # -----------------------------------------------------------------------

    def test_happy_path_opp_hint_as_sf_id_skips_soql(
        self, monkeypatch, mock_audit, mock_sf_identity
    ):
        """opp_hint is a valid 18-char SF Opportunity ID → skips SOQL, creates Task directly."""
        opp_id_hint = "006" + "A" * 15  # 18 chars starting with 006

        call_tool_mock = MagicMock(return_value={"id": TASK_ID_1, "success": True})
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint=opp_id_hint,
            subject=VALID_SUBJECT,
            notes="Good call",
            when="2025-06-01",
        )

        assert result["status"] == "ok"
        assert result["task_id"] == TASK_ID_1
        assert result["opp_id"] == opp_id_hint

        # No SOQL or SOSL call — only sf_create_record
        all_tool_names = [c[0][0] for c in call_tool_mock.call_args_list]
        assert "sf_soql_query" not in all_tool_names
        assert "sf_search" not in all_tool_names
        assert "sf_create_record" in all_tool_names

    def test_happy_path_opp_hint_name_fragment_soql(
        self, monkeypatch, mock_audit, mock_sf_identity
    ):
        """opp_hint is a name fragment → SOQL LIKE search → one match → Task created."""
        soql_call_count = {"n": 0}

        def side_effect(tool, params):
            if tool == "sf_soql_query":
                soql_call_count["n"] += 1
                return _soql_one_opp()
            if tool == "sf_create_record":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ok"
        assert result["task_id"] == TASK_ID_1
        assert soql_call_count["n"] == 1

    def test_happy_path_person_hint_resolves_via_sosl_then_soql(
        self, monkeypatch, mock_audit, mock_sf_identity
    ):
        """person_hint → SOSL person lookup → SOQL open opp by AccountId → Task created."""
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "sf_soql_query":
                return _soql_one_opp()
            if tool == "sf_create_record":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ok"
        assert result["task_id"] == TASK_ID_1
        assert result["opp_id"] == OPP_ID

    # -----------------------------------------------------------------------
    # Validation failures
    # -----------------------------------------------------------------------

    def test_invalid_subject_returns_before_sf_calls(self, monkeypatch):
        call_tool_mock = MagicMock()
        monkeypatch.setattr(sf_intents, "call_tool", call_tool_mock)

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="006" + "A" * 15,
            subject="Skywriting",
        )

        assert result["status"] == "invalid_subject"
        assert result["subject"] == "Skywriting"
        call_tool_mock.assert_not_called()

    # -----------------------------------------------------------------------
    # No open opps
    # -----------------------------------------------------------------------

    def test_no_open_opps_opp_hint_name_fragment(self, monkeypatch, mock_sf_identity):
        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(return_value=_soql_empty()))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="NonExistentOpp",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "no_open_opps"
        assert "message" in result

    def test_no_open_opps_when_person_not_found(self, monkeypatch, mock_sf_identity):
        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(return_value=_sosl_empty()))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Unknown Person",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "no_open_opps"

    def test_no_open_opps_person_found_but_no_opps(self, monkeypatch, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "sf_soql_query":
                return _soql_empty()
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "no_open_opps"

    # -----------------------------------------------------------------------
    # Ambiguous opp
    # -----------------------------------------------------------------------

    def test_ambiguous_opp_returns_up_to_five_candidates(self, monkeypatch, mock_sf_identity):
        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(return_value=_soql_multi_opp()))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ambiguous_opp"
        assert 1 <= len(result["candidates"]) <= 5
        for c in result["candidates"]:
            assert "id" in c
            assert "name" in c
            assert "stage" in c

    def test_ambiguous_opp_person_hint_path(self, monkeypatch, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_search":
                return _sosl_one_person()
            if tool == "sf_soql_query":
                return _soql_multi_opp()
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            person_hint="Alice",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ambiguous_opp"
        assert len(result["candidates"]) <= 5

    # -----------------------------------------------------------------------
    # SF identity failure
    # -----------------------------------------------------------------------

    def test_no_sf_identity_with_direct_opp_id_omits_owner(self, monkeypatch, mock_sf_identity_none):
        """Missing Slack→SF mapping is non-fatal: log_touch still writes the
        Task, with OwnerId omitted so SF defaults to the API user."""
        opp_id_hint = "006" + "A" * 15
        calls = []

        def side_effect(tool, params):
            calls.append((tool, params))
            return {"id": TASK_ID_1, "success": True}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint=opp_id_hint,
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ok"
        task_create = next(
            (params for tool, params in calls if tool == "sf_create_record" and params["object_name"] == "Task"),
            None,
        )
        assert task_create is not None
        assert "OwnerId" not in task_create["data"]

    def test_no_sf_identity_with_name_fragment_omits_owner(self, monkeypatch, mock_sf_identity_none):
        calls = []

        def side_effect(tool, params):
            calls.append((tool, params))
            if tool == "sf_soql_query":
                return _soql_one_opp()
            return {"id": TASK_ID_1, "success": True}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "ok"
        task_create = next(
            (params for tool, params in calls if tool == "sf_create_record" and params["object_name"] == "Task"),
            None,
        )
        assert task_create is not None
        assert "OwnerId" not in task_create["data"]

    # -----------------------------------------------------------------------
    # Task creation failure
    # -----------------------------------------------------------------------

    def test_create_failed_task_returns_no_id(self, monkeypatch, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_soql_query":
                return _soql_one_opp()
            if tool == "sf_create_record":
                return {"id": None, "success": False, "errors": ["FIELD_REQUIRED"]}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "create_failed"

    def test_create_failed_task_raises(self, monkeypatch, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_soql_query":
                return _soql_one_opp()
            raise RuntimeError("Connection timeout")

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "create_failed"
        assert "Connection timeout" in result["error"]

    def test_create_failed_direct_id_task_raises(self, monkeypatch, mock_sf_identity):
        """Task create raises even when opp is known by direct SF ID."""
        opp_id_hint = "006" + "A" * 15

        monkeypatch.setattr(
            sf_intents, "call_tool", MagicMock(side_effect=RuntimeError("timeout"))
        )

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint=opp_id_hint,
            subject=VALID_SUBJECT,
        )

        assert result["status"] == "create_failed"

    # -----------------------------------------------------------------------
    # Audit and opp_url assertions
    # -----------------------------------------------------------------------

    def test_audit_called_with_correct_keys(self, monkeypatch, mock_audit, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_soql_query":
                return _soql_one_opp()
            if tool == "sf_create_record":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
            notes="Great conversation",
        )

        mock_audit.assert_called_once()
        record = mock_audit.call_args[0][0]
        assert record["tool"] == "sf_log_touch"
        assert record["slack_user"] == SLACK_USER_ID
        assert record["sf_user"] == SF_USER_ID
        assert record["opp_id"] == OPP_ID
        assert record["task_id"] == TASK_ID_1
        assert record["subject"] == VALID_SUBJECT

    def test_opp_url_format_in_result(self, monkeypatch, mock_audit, mock_sf_identity):
        def side_effect(tool, params):
            if tool == "sf_soql_query":
                return _soql_one_opp()
            if tool == "sf_create_record":
                return {"id": TASK_ID_1, "success": True}
            return {}

        monkeypatch.setattr(sf_intents, "call_tool", MagicMock(side_effect=side_effect))

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint="Alice Smith",
            subject=VALID_SUBJECT,
        )

        assert result["opp_url"] == f"{INSTANCE_URL}/lightning/r/Opportunity/{OPP_ID}/view"

    def test_opp_url_with_direct_sf_id(self, monkeypatch, mock_audit, mock_sf_identity):
        """opp_url is built from the direct opp_hint when it's a valid SF ID."""
        opp_id_hint = "006" + "A" * 15
        monkeypatch.setattr(
            sf_intents, "call_tool", MagicMock(return_value={"id": TASK_ID_1, "success": True})
        )

        result = sf_intents.log_touch(
            slack_user_id=SLACK_USER_ID,
            slack_client=_make_slack_client(),
            opp_hint=opp_id_hint,
            subject=VALID_SUBJECT,
        )

        expected_url = f"{INSTANCE_URL}/lightning/r/Opportunity/{opp_id_hint}/view"
        assert result["opp_url"] == expected_url
