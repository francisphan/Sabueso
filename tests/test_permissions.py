"""Tests for the permissions / ACL module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import permissions
from permissions import (
    Role,
    AclEntry,
    add_user,
    bulk_add_users,
    can_use_tool,
    check_access,
    get_role,
    get_sf_user_override,
    is_admin,
    is_authorized,
    list_users,
    map_sf_user,
    parse_admin_command,
    remove_user,
    unmap_sf_user,
)

ADMIN_ID = "UADMIN"
READER_ID = "UREADER"
SALES_REP_ID = "USALESREP"
UNKNOWN_ID = "UUNKNOWN"


@pytest.fixture(autouse=True)
def isolated_acl(tmp_path, monkeypatch):
    """Each test gets a fresh ACL file with one admin."""
    acl_file = tmp_path / "acl.json"
    acl_file.write_text(json.dumps({ADMIN_ID: "admin"}))
    monkeypatch.setattr(permissions, "_ACL_PATH", acl_file)
    monkeypatch.setattr(permissions, "_DEFAULT_ADMIN", ADMIN_ID)
    return acl_file


# ── Role lookups ───────────────────────────────────────────────────────────

class TestGetRole:
    def test_admin(self):
        assert get_role(ADMIN_ID) == Role.ADMIN

    def test_unknown_user(self):
        assert get_role(UNKNOWN_ID) is None

    def test_read_only(self):
        add_user(READER_ID, Role.READ_ONLY)
        assert get_role(READER_ID) == Role.READ_ONLY

    def test_corrupt_role_returns_none(self, isolated_acl):
        acl = json.loads(isolated_acl.read_text())
        acl["UBAD"] = "superuser"
        isolated_acl.write_text(json.dumps(acl))
        assert get_role("UBAD") is None


class TestHelpers:
    def test_is_authorized(self):
        assert is_authorized(ADMIN_ID) is True
        assert is_authorized(UNKNOWN_ID) is False

    def test_is_admin(self):
        assert is_admin(ADMIN_ID) is True
        add_user(READER_ID, Role.READ_ONLY)
        assert is_admin(READER_ID) is False

    def test_can_use_tool_admin(self):
        assert can_use_tool(ADMIN_ID, "sf_create_opportunity_for_person") is True

    def test_can_use_tool_read_only(self):
        add_user(READER_ID, Role.READ_ONLY)
        assert can_use_tool(READER_ID, "sf_create_opportunity_for_person") is False

    def test_can_use_tool_unknown_user(self):
        assert can_use_tool(UNKNOWN_ID, "sf_create_opportunity_for_person") is False


# ── Access checks ──────────────────────────────────────────────────────────

class TestCheckAccess:
    def test_authorized_user_passes(self):
        assert check_access(ADMIN_ID) is None

    def test_unknown_user_denied(self):
        msg = check_access(UNKNOWN_ID)
        assert msg is not None
        assert "don't have you on my list" in msg

    def test_read_only_user_on_list_but_cannot_use_write_tool(self):
        add_user(READER_ID, Role.READ_ONLY)
        # check_access only verifies the user is on the list
        assert check_access(READER_ID) is None
        # Tool-level authorization is separate
        assert can_use_tool(READER_ID, "sf_create_opportunity_for_person") is False

    def test_admin_can_use_write_tool(self):
        assert can_use_tool(ADMIN_ID, "sf_create_opportunity_for_person") is True


# ── can_use_tool: role-based scopes ────────────────────────────────────────

class TestCanUseTool:
    def test_sales_rep_can_use_create_opportunity(self):
        add_user(SALES_REP_ID, Role.SALES_REP)
        assert can_use_tool(SALES_REP_ID, "sf_create_opportunity_for_person") is True

    def test_sales_rep_can_use_log_touch(self):
        add_user(SALES_REP_ID, Role.SALES_REP)
        assert can_use_tool(SALES_REP_ID, "sf_log_touch") is True

    def test_sales_rep_cannot_use_arbitrary_write(self):
        add_user(SALES_REP_ID, Role.SALES_REP)
        assert can_use_tool(SALES_REP_ID, "sf_delete_record") is False

    def test_read_only_cannot_use_any_write(self):
        add_user(READER_ID, Role.READ_ONLY)
        assert can_use_tool(READER_ID, "sf_create_opportunity_for_person") is False
        assert can_use_tool(READER_ID, "sf_log_touch") is False

    def test_admin_can_use_any_tool(self):
        assert can_use_tool(ADMIN_ID, "sf_create_opportunity_for_person") is True
        assert can_use_tool(ADMIN_ID, "sf_log_touch") is True
        assert can_use_tool(ADMIN_ID, "sf_delete_record") is True
        assert can_use_tool(ADMIN_ID, "any_arbitrary_tool") is True

    def test_extra_grant_extends_read_only(self, isolated_acl):
        """A read_only user with extra=['sf_log_touch'] can use that tool."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {"role": "read_only", "extra": ["sf_log_touch"]}
        isolated_acl.write_text(json.dumps(acl))
        assert can_use_tool(READER_ID, "sf_log_touch") is True
        # But not tools they haven't been granted
        assert can_use_tool(READER_ID, "sf_delete_record") is False

    def test_deny_revokes_role_scope(self, isolated_acl):
        """A sales_rep with deny=['sf_log_touch'] cannot use that tool."""
        acl = json.loads(isolated_acl.read_text())
        acl[SALES_REP_ID] = {"role": "sales_rep", "deny": ["sf_log_touch"]}
        isolated_acl.write_text(json.dumps(acl))
        assert can_use_tool(SALES_REP_ID, "sf_log_touch") is False
        # Other role-scoped tools still work
        assert can_use_tool(SALES_REP_ID, "sf_create_opportunity_for_person") is True

    def test_deny_wins_over_extra(self, isolated_acl):
        """deny overrides extra: user gets neither."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {
            "role": "read_only",
            "extra": ["sf_log_touch"],
            "deny": ["sf_log_touch"],
        }
        isolated_acl.write_text(json.dumps(acl))
        assert can_use_tool(READER_ID, "sf_log_touch") is False


# ── ACL object form ─────────────────────────────────────────────────────────

class TestAclObjectForm:
    def test_object_form_round_trips(self, isolated_acl):
        """Object-form ACL entry survives save and reload."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {
            "role": "read_only",
            "extra": ["sf_log_touch"],
            "deny": ["sf_create_opportunity_for_person"],
            "sf_user_id": "005xx0000012345",
        }
        isolated_acl.write_text(json.dumps(acl))

        assert get_role(READER_ID) == Role.READ_ONLY
        assert can_use_tool(READER_ID, "sf_log_touch") is True
        assert can_use_tool(READER_ID, "sf_create_opportunity_for_person") is False
        assert get_sf_user_override(READER_ID) == "005xx0000012345"

    def test_compact_form_works(self, isolated_acl):
        """A plain string value (compact form) is parsed correctly."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = "read_only"
        isolated_acl.write_text(json.dumps(acl))
        assert get_role(READER_ID) == Role.READ_ONLY

    def test_map_sf_user_upgrades_compact_to_object(self):
        """Mapping an SF user ID makes the entry non-compact."""
        add_user(READER_ID, Role.READ_ONLY)
        # Initially compact
        assert get_sf_user_override(READER_ID) is None

        map_sf_user(READER_ID, "005abc")

        # Now has sf_user_id
        assert get_sf_user_override(READER_ID) == "005abc"

        # Persisted file should have object form
        from permissions import _ACL_PATH
        raw = json.loads(_ACL_PATH.read_text())
        assert isinstance(raw[READER_ID], dict)
        assert raw[READER_ID]["sf_user_id"] == "005abc"

    def test_unmap_sf_user_downgrades_to_compact_when_no_other_overrides(self, isolated_acl):
        """Clearing sf_user_id on an entry with no other overrides writes compact form."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {"role": "read_only", "sf_user_id": "005abc"}
        isolated_acl.write_text(json.dumps(acl))

        unmap_sf_user(READER_ID)

        raw = json.loads(isolated_acl.read_text())
        # With no extra/deny/sf_user_id, should be compact (plain string)
        assert raw[READER_ID] == "read_only"

    def test_unmap_sf_user_stays_object_when_other_overrides_present(self, isolated_acl):
        """Clearing sf_user_id keeps object form if extra or deny are still set."""
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {
            "role": "read_only",
            "extra": ["sf_log_touch"],
            "sf_user_id": "005abc",
        }
        isolated_acl.write_text(json.dumps(acl))

        unmap_sf_user(READER_ID)

        raw = json.loads(isolated_acl.read_text())
        # Still has extra, so stays object form
        assert isinstance(raw[READER_ID], dict)
        assert "sf_user_id" not in raw[READER_ID]
        assert raw[READER_ID]["extra"] == ["sf_log_touch"]


# ── get_sf_user_override ────────────────────────────────────────────────────

class TestGetSfUserOverride:
    def test_returns_none_when_not_mapped(self):
        add_user(READER_ID, Role.READ_ONLY)
        assert get_sf_user_override(READER_ID) is None

    def test_returns_none_for_unknown_user(self):
        assert get_sf_user_override(UNKNOWN_ID) is None

    def test_returns_value_when_mapped(self):
        add_user(READER_ID, Role.READ_ONLY)
        map_sf_user(READER_ID, "005xx0000012345")
        assert get_sf_user_override(READER_ID) == "005xx0000012345"


# ── map_sf_user / unmap_sf_user ─────────────────────────────────────────────

class TestMapUnmapSfUser:
    def test_map_sets_sf_user_id(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = map_sf_user(READER_ID, "005abc")
        assert "005abc" in result
        assert get_sf_user_override(READER_ID) == "005abc"

    def test_map_unknown_user_returns_error(self):
        result = map_sf_user(UNKNOWN_ID, "005abc")
        assert "not in the access list" in result
        assert get_sf_user_override(UNKNOWN_ID) is None

    def test_unmap_clears_sf_user_id(self):
        add_user(READER_ID, Role.READ_ONLY)
        map_sf_user(READER_ID, "005abc")
        result = unmap_sf_user(READER_ID)
        assert "Cleared" in result
        assert get_sf_user_override(READER_ID) is None

    def test_unmap_when_no_mapping_returns_message(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = unmap_sf_user(READER_ID)
        assert "no SF user mapping" in result

    def test_unmap_unknown_user_returns_error(self):
        result = unmap_sf_user(UNKNOWN_ID)
        assert "not in the access list" in result


# ── add_user preserves overrides ─────────────────────────────────────────────

class TestAddUserPreservesOverrides:
    def test_update_role_preserves_sf_user_id(self):
        add_user(READER_ID, Role.READ_ONLY)
        map_sf_user(READER_ID, "005abc")
        # Update role
        add_user(READER_ID, Role.SALES_REP)
        # sf_user_id should still be set
        assert get_sf_user_override(READER_ID) == "005abc"
        assert get_role(READER_ID) == Role.SALES_REP

    def test_update_role_preserves_extra_and_deny(self, isolated_acl):
        acl = json.loads(isolated_acl.read_text())
        acl[READER_ID] = {
            "role": "read_only",
            "extra": ["sf_log_touch"],
            "deny": ["sf_create_opportunity_for_person"],
        }
        isolated_acl.write_text(json.dumps(acl))

        add_user(READER_ID, Role.SALES_REP)

        raw = json.loads(isolated_acl.read_text())
        assert raw[READER_ID]["extra"] == ["sf_log_touch"]
        assert raw[READER_ID]["deny"] == ["sf_create_opportunity_for_person"]
        assert raw[READER_ID]["role"] == "sales_rep"


# ── User management ───────────────────────────────────────────────────────

class TestAddRemoveUser:
    def test_add_new_user(self):
        result = add_user(READER_ID, Role.READ_ONLY)
        assert result.startswith("Added")
        assert is_authorized(READER_ID)

    def test_update_existing_user(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = add_user(READER_ID, Role.ADMIN)
        assert result.startswith("Updated")
        assert is_admin(READER_ID)

    def test_remove_user(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = remove_user(READER_ID)
        assert result.startswith("Removed")
        assert not is_authorized(READER_ID)

    def test_remove_nonexistent_user(self):
        result = remove_user(UNKNOWN_ID)
        assert "not in the access list" in result

    def test_cannot_remove_bootstrap_admin(self):
        result = remove_user(ADMIN_ID)
        assert "Cannot remove" in result
        assert is_admin(ADMIN_ID)

    def test_add_same_role_is_idempotent(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = add_user(READER_ID, Role.READ_ONLY)
        assert "Updated" in result
        assert get_role(READER_ID) == Role.READ_ONLY


class TestListUsers:
    def test_list_with_users(self):
        result = list_users()
        assert ADMIN_ID in result
        assert "admin" in result

    def test_list_empty(self, isolated_acl):
        isolated_acl.write_text(json.dumps({}))
        assert "No users" in list_users()


# ── Admin command parsing ──────────────────────────────────────────────────

class TestParseAdminCommand:
    def test_non_admin_rejected(self):
        result = parse_admin_command("!access list", UNKNOWN_ID)
        assert result == "Only admins can manage access."

    def test_not_a_command(self):
        assert parse_admin_command("hello", ADMIN_ID) is None

    def test_list(self):
        result = parse_admin_command("!access list", ADMIN_ID)
        assert "Authorized users" in result

    def test_add_simple_mention(self):
        result = parse_admin_command("!access add <@UNEW> read_only", ADMIN_ID)
        assert "Added" in result
        assert is_authorized("UNEW")

    def test_add_mention_with_display_name(self):
        """Slack sends <@U12345|username> — the |username must be stripped."""
        result = parse_admin_command("!access add <@UNEW|alice> read_only", ADMIN_ID)
        assert "Added" in result
        assert is_authorized("UNEW")
        # Must NOT store the polluted ID
        assert not is_authorized("UNEW|alice")

    def test_add_invalid_role(self):
        result = parse_admin_command("!access add <@UNEW> superuser", ADMIN_ID)
        assert "Unknown role" in result

    def test_remove_with_display_name(self):
        add_user("UTARGET", Role.READ_ONLY)
        result = parse_admin_command("!access remove <@UTARGET|bob>", ADMIN_ID)
        assert "Removed" in result
        assert not is_authorized("UTARGET")

    def test_incomplete_command_shows_help(self):
        result = parse_admin_command("!access", ADMIN_ID)
        assert "Access management commands" in result

    def test_unknown_subcommand_shows_help(self):
        result = parse_admin_command("!access foobar", ADMIN_ID)
        assert "Access management commands" in result

    def test_extra_whitespace_in_command(self):
        result = parse_admin_command("!access  add  <@UNEW>  read_only", ADMIN_ID)
        assert "Added" in result
        assert is_authorized("UNEW")

    def test_add_role_case_insensitive(self):
        result = parse_admin_command("!access add <@UNEW> ADMIN", ADMIN_ID)
        assert "Added" in result
        assert is_admin("UNEW")

    def test_add_empty_mention(self):
        """<@> with nothing inside should not crash."""
        result = parse_admin_command("!access add <@> read_only", ADMIN_ID)
        assert result is not None  # should either add empty or show help, not crash

    def test_bulk_add_three_users(self):
        result = parse_admin_command(
            "!access bulk-add read_only <@UA> <@UB> <@UC>", ADMIN_ID
        )
        assert "Added (3)" in result
        for uid in ("UA", "UB", "UC"):
            assert get_role(uid) == Role.READ_ONLY

    def test_bulk_add_with_display_names(self):
        result = parse_admin_command(
            "!access bulk-add read_only <@UA|alice> <@UB|bob>", ADMIN_ID
        )
        assert "Added (2)" in result
        assert is_authorized("UA")
        assert is_authorized("UB")
        assert not is_authorized("UA|alice")

    def test_bulk_add_mixed_new_and_existing(self):
        add_user("UEXISTING", Role.READ_ONLY)
        result = parse_admin_command(
            "!access bulk-add read_only <@UEXISTING> <@UNEW>", ADMIN_ID
        )
        assert "Added (1)" in result
        assert "Already on list (1)" in result

    def test_bulk_add_invalid_role(self):
        result = parse_admin_command("!access bulk-add superuser <@UA> <@UB>", ADMIN_ID)
        assert "Unknown role" in result
        assert not is_authorized("UA")

    def test_bulk_add_no_mentions_shows_help(self):
        result = parse_admin_command("!access bulk-add read_only", ADMIN_ID)
        assert "Access management commands" in result

    def test_bulk_add_non_admin_rejected(self):
        result = parse_admin_command(
            "!access bulk-add read_only <@UA> <@UB>", UNKNOWN_ID
        )
        assert result == "Only admins can manage access."
        assert not is_authorized("UA")

    def test_map_command(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = parse_admin_command(f"!access map <@{READER_ID}> 005xx0000012345", ADMIN_ID)
        assert "005xx0000012345" in result
        assert get_sf_user_override(READER_ID) == "005xx0000012345"

    def test_map_command_with_display_name(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = parse_admin_command(
            f"!access map <@{READER_ID}|alice> 005xx0000012345", ADMIN_ID
        )
        assert "005xx0000012345" in result
        assert get_sf_user_override(READER_ID) == "005xx0000012345"

    def test_map_command_non_admin_rejected(self):
        add_user(READER_ID, Role.READ_ONLY)
        result = parse_admin_command(
            f"!access map <@{READER_ID}> 005xx0000012345", UNKNOWN_ID
        )
        assert result == "Only admins can manage access."

    def test_unmap_command(self):
        add_user(READER_ID, Role.READ_ONLY)
        map_sf_user(READER_ID, "005xx0000012345")
        result = parse_admin_command(f"!access unmap <@{READER_ID}>", ADMIN_ID)
        assert "Cleared" in result
        assert get_sf_user_override(READER_ID) is None

    def test_unmap_command_with_display_name(self):
        add_user(READER_ID, Role.READ_ONLY)
        map_sf_user(READER_ID, "005xx0000012345")
        result = parse_admin_command(
            f"!access unmap <@{READER_ID}|alice>", ADMIN_ID
        )
        assert "Cleared" in result
        assert get_sf_user_override(READER_ID) is None

    def test_unmap_command_non_admin_rejected(self):
        result = parse_admin_command(f"!access unmap <@{READER_ID}>", UNKNOWN_ID)
        assert result == "Only admins can manage access."


class TestBulkAddUsers:
    def test_bulk_add_basic(self):
        result = bulk_add_users(["U1", "U2"], Role.READ_ONLY)
        assert "Added (2)" in result
        assert is_authorized("U1")
        assert is_authorized("U2")

    def test_bulk_add_skips_empty_ids(self):
        result = bulk_add_users(["U1", "", "U2"], Role.READ_ONLY)
        assert "Added (2)" in result
        assert not is_authorized("")


# ── ACL file edge cases ───────────────────────────────────────────────────

class TestAclFile:
    def test_missing_file_uses_default_admin(self, isolated_acl):
        isolated_acl.unlink()
        assert is_admin(ADMIN_ID)

    def test_corrupt_file_uses_default_admin(self, isolated_acl):
        isolated_acl.write_text("not json {{{")
        assert is_admin(ADMIN_ID)
