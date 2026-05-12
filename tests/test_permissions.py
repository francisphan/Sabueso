"""Tests for the permissions / ACL module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import permissions
from permissions import (
    Role,
    add_user,
    bulk_add_users,
    can_write,
    check_access,
    get_role,
    is_admin,
    is_authorized,
    list_users,
    parse_admin_command,
    remove_user,
)

ADMIN_ID = "UADMIN"
READER_ID = "UREADER"
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

    def test_can_write(self):
        assert can_write(ADMIN_ID) is True
        add_user(READER_ID, Role.READ_ONLY)
        assert can_write(READER_ID) is False
        assert can_write(UNKNOWN_ID) is False


# ── Access checks ──────────────────────────────────────────────────────────

class TestCheckAccess:
    def test_authorized_user_passes(self):
        assert check_access(ADMIN_ID) is None

    def test_unknown_user_denied(self):
        msg = check_access(UNKNOWN_ID)
        assert msg is not None
        assert "don't have you on my list" in msg

    def test_read_only_user_denied_write(self):
        add_user(READER_ID, Role.READ_ONLY)
        assert check_access(READER_ID) is None
        msg = check_access(READER_ID, is_write_operation=True)
        assert msg is not None
        assert "read-only" in msg

    def test_admin_can_write(self):
        assert check_access(ADMIN_ID, is_write_operation=True) is None


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
