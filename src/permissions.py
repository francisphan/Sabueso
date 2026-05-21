"""
Role-based access control for Sabueso.

Roles:
  - admin:     full read + write; can manage the access list.
  - sales_rep: read + a narrow write scope (opportunity + touch intent tools).
  - read_only: read-only.

Users not in the access list are denied entirely.

The ACL is stored as JSON. Two entry shapes are supported:

    "U123": "admin"                              # role only (compact)
    "U456": {"role": "sales_rep",                # role + per-user override(s)
             "extra": ["sf_log_touch"],
             "deny": [],
             "sf_user_id": "005xx0000012345"}

The compact form is used when no overrides are set, so existing ACL files
stay valid after the upgrade.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class Role(str, Enum):
    ADMIN = "admin"
    SALES_REP = "sales_rep"
    READ_ONLY = "read_only"


# Per-role write tool scopes. Admin is granted everything implicitly (see
# can_use_tool); only non-admin roles need explicit entries here. Adding a
# new write tool? Decide here who can call it.
ROLE_TOOL_SCOPES: dict[Role, set[str]] = {
    Role.SALES_REP: {"sf_create_opportunity_for_person", "sf_log_touch"},
    Role.READ_ONLY: set(),
}


@dataclass
class AclEntry:
    role: Role
    extra: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    sf_user_id: str | None = None

    def is_compact(self) -> bool:
        return not self.extra and not self.deny and self.sf_user_id is None


_ACL_PATH = Path(os.getenv("ACL_FILE", "acl.json"))
_DEFAULT_ADMIN = os.getenv("BOT_ADMIN_USER_ID", "U0ACKBHM2S1")
_acl_lock = threading.Lock()


# ── Load / save ─────────────────────────────────────────────────────────────

def _parse_entry(raw: object) -> AclEntry | None:
    """Normalize one ACL value (string or object) into an AclEntry."""
    if isinstance(raw, str):
        try:
            return AclEntry(role=Role(raw))
        except ValueError:
            log.warning("Unknown role string %r in ACL", raw)
            return None
    if isinstance(raw, dict):
        try:
            role = Role(raw["role"])
        except (KeyError, ValueError):
            log.warning("Invalid ACL object missing/unknown role: %r", raw)
            return None
        return AclEntry(
            role=role,
            extra=list(raw.get("extra", [])),
            deny=list(raw.get("deny", [])),
            sf_user_id=raw.get("sf_user_id") or None,
        )
    log.warning("Unsupported ACL value type %r", type(raw))
    return None


def _serialize_entry(entry: AclEntry) -> str | dict:
    if entry.is_compact():
        return entry.role.value
    out: dict = {"role": entry.role.value}
    if entry.extra:
        out["extra"] = entry.extra
    if entry.deny:
        out["deny"] = entry.deny
    if entry.sf_user_id:
        out["sf_user_id"] = entry.sf_user_id
    return out


def _load_acl() -> dict[str, AclEntry]:
    if _ACL_PATH.exists():
        try:
            raw = json.loads(_ACL_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt ACL file at %s — falling back to defaults", _ACL_PATH)
            return {_DEFAULT_ADMIN: AclEntry(role=Role.ADMIN)}
        if not isinstance(raw, dict):
            log.warning(
                "ACL file at %s has wrong shape (got %s, expected object) — falling back to defaults",
                _ACL_PATH, type(raw).__name__,
            )
            return {_DEFAULT_ADMIN: AclEntry(role=Role.ADMIN)}
        entries: dict[str, AclEntry] = {}
        for uid, value in raw.items():
            parsed = _parse_entry(value)
            if parsed is not None:
                entries[uid] = parsed
        return entries

    # Fresh install: seed the bootstrap admin and persist.
    bootstrap = {_DEFAULT_ADMIN: AclEntry(role=Role.ADMIN)}
    try:
        _save_acl(bootstrap)
        log.info("Bootstrapped ACL at %s with admin %s", _ACL_PATH, _DEFAULT_ADMIN)
    except OSError as exc:
        log.warning("Could not persist bootstrap ACL to %s (%s) — running in-memory", _ACL_PATH, exc)
    return bootstrap


def _save_acl(acl: dict[str, AclEntry]) -> None:
    """Atomic write: serialize to a sibling temp file then rename into place.

    Prevents a mid-write crash from leaving the ACL truncated or corrupt
    (which would silently revoke every non-bootstrap user on next start).
    """
    _ACL_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialized = {uid: _serialize_entry(entry) for uid, entry in acl.items()}
    tmp = _ACL_PATH.with_suffix(_ACL_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(serialized, indent=2))
    tmp.replace(_ACL_PATH)
    log.info("ACL saved to %s", _ACL_PATH)


# ── Lookups ─────────────────────────────────────────────────────────────────

def _get_entry(user_id: str) -> AclEntry | None:
    with _acl_lock:
        acl = _load_acl()
    return acl.get(user_id)


def get_role(user_id: str) -> Role | None:
    entry = _get_entry(user_id)
    return entry.role if entry else None


def is_authorized(user_id: str) -> bool:
    return _get_entry(user_id) is not None


def is_admin(user_id: str) -> bool:
    return get_role(user_id) == Role.ADMIN


def get_sf_user_override(user_id: str) -> str | None:
    """Return the manually-mapped SF User ID for this Slack user, if any."""
    entry = _get_entry(user_id)
    return entry.sf_user_id if entry else None


def can_use_tool(user_id: str, tool_name: str) -> bool:
    """Whether this user is authorized to invoke a given tool."""
    entry = _get_entry(user_id)
    if entry is None:
        return False
    if entry.role == Role.ADMIN:
        return True
    base = ROLE_TOOL_SCOPES.get(entry.role, set())
    effective = (base | set(entry.extra)) - set(entry.deny)
    return tool_name in effective


def check_access(user_id: str) -> str | None:
    """Auth check: is this user on the list at all?

    Returns a user-facing denial message, or None if allowed.
    Per-tool authorization is a separate call (can_use_tool).
    """
    if not is_authorized(user_id):
        return (
            "Sorry, I don't have you on my list. "
            "Ask an admin to grant you access and I'll start sniffing things out for you."
        )
    return None


# ── Mutations ───────────────────────────────────────────────────────────────

def add_user(user_id: str, role: Role) -> str:
    """Set a user's role. Preserves any existing overrides (extra/deny/sf_user_id)."""
    with _acl_lock:
        acl = _load_acl()
        existing = acl.get(user_id)
        was_existing = existing is not None
        if existing is not None:
            existing.role = role
        else:
            acl[user_id] = AclEntry(role=role)
        _save_acl(acl)
    action = "Updated" if was_existing else "Added"
    return f"{action} <@{user_id}> with *{role.value}* access."


def bulk_add_users(user_ids: list[str], role: Role) -> str:
    added: list[str] = []
    updated: list[str] = []
    with _acl_lock:
        acl = _load_acl()
        for uid in user_ids:
            if not uid:
                continue
            existing = acl.get(uid)
            if existing is not None:
                existing.role = role
                updated.append(uid)
            else:
                acl[uid] = AclEntry(role=role)
                added.append(uid)
        _save_acl(acl)

    lines = [f"Bulk-add with *{role.value}* access:"]
    if added:
        lines.append(f"• Added ({len(added)}): " + " ".join(f"<@{u}>" for u in added))
    if updated:
        lines.append(f"• Already on list ({len(updated)}): " + " ".join(f"<@{u}>" for u in updated))
    if not added and not updated:
        lines.append("• No valid user mentions found.")
    return "\n".join(lines)


def remove_user(user_id: str) -> str:
    with _acl_lock:
        acl = _load_acl()
        if user_id not in acl:
            return f"<@{user_id}> is not in the access list."
        if user_id == _DEFAULT_ADMIN:
            return "Cannot remove the bootstrap admin."
        del acl[user_id]
        _save_acl(acl)
    return f"Removed <@{user_id}> from the access list."


def map_sf_user(user_id: str, sf_user_id: str) -> str:
    with _acl_lock:
        acl = _load_acl()
        entry = acl.get(user_id)
        if entry is None:
            return f"<@{user_id}> is not in the access list. Add them first."
        entry.sf_user_id = sf_user_id
        _save_acl(acl)
    return f"Mapped <@{user_id}> to Salesforce user `{sf_user_id}`."


def unmap_sf_user(user_id: str) -> str:
    with _acl_lock:
        acl = _load_acl()
        entry = acl.get(user_id)
        if entry is None:
            return f"<@{user_id}> is not in the access list."
        if entry.sf_user_id is None:
            return f"<@{user_id}> has no SF user mapping to clear."
        entry.sf_user_id = None
        _save_acl(acl)
    return f"Cleared SF user mapping for <@{user_id}>."


def list_users() -> str:
    with _acl_lock:
        acl = _load_acl()
    if not acl:
        return "No users in the access list."
    lines = []
    for uid, entry in sorted(acl.items(), key=lambda x: x[1].role.value):
        suffix = ""
        annotations: list[str] = []
        if entry.sf_user_id:
            annotations.append(f"SF: `{entry.sf_user_id}`")
        if entry.extra:
            annotations.append("extra: " + ", ".join(f"`{t}`" for t in entry.extra))
        if entry.deny:
            annotations.append("deny: " + ", ".join(f"`{t}`" for t in entry.deny))
        if annotations:
            suffix = " (" + "; ".join(annotations) + ")"
        lines.append(f"• <@{uid}> — `{entry.role.value}`{suffix}")
    return "*Authorized users:*\n" + "\n".join(lines)


# ── Admin command parser ────────────────────────────────────────────────────

def _parse_user_mention(token: str) -> str:
    """Extract user ID from a `<@U12345>` or `<@U12345|name>` token."""
    return token.strip("<@>").split("|")[0]


def parse_admin_command(text: str, requesting_user_id: str) -> str | None:
    """Parse admin commands from message text.

    Supported:
        !access list
        !access add <@U12345> read_only|sales_rep|admin
        !access bulk-add read_only|sales_rep|admin <@U1> <@U2> ...
        !access remove <@U12345>
        !access map <@U12345> <sf_user_id>
        !access unmap <@U12345>
    """
    text = text.strip()
    if not text.startswith("!access"):
        return None

    if not is_admin(requesting_user_id):
        return "Only admins can manage access."

    parts = text.split()
    if len(parts) < 2:
        return _admin_help()

    cmd = parts[1].lower()

    if cmd == "list":
        return list_users()

    if cmd == "add" and len(parts) >= 4:
        target = _parse_user_mention(parts[2])
        role_str = parts[3].lower()
        try:
            role = Role(role_str)
        except ValueError:
            return f"Unknown role `{role_str}`. Use `admin`, `sales_rep`, or `read_only`."
        return add_user(target, role)

    if cmd == "bulk-add" and len(parts) >= 4:
        role_str = parts[2].lower()
        try:
            role = Role(role_str)
        except ValueError:
            return f"Unknown role `{role_str}`. Use `admin`, `sales_rep`, or `read_only`."
        user_ids = [_parse_user_mention(p) for p in parts[3:]]
        return bulk_add_users(user_ids, role)

    if cmd == "remove" and len(parts) >= 3:
        target = _parse_user_mention(parts[2])
        return remove_user(target)

    if cmd == "map" and len(parts) >= 4:
        target = _parse_user_mention(parts[2])
        sf_user_id = parts[3].strip("`")
        return map_sf_user(target, sf_user_id)

    if cmd == "unmap" and len(parts) >= 3:
        target = _parse_user_mention(parts[2])
        return unmap_sf_user(target)

    return _admin_help()


def _admin_help() -> str:
    return (
        "*Access management commands:*\n"
        "• `!access list` — show authorized users\n"
        "• `!access add @user admin|sales_rep|read_only` — grant access\n"
        "• `!access bulk-add admin|sales_rep|read_only @user1 @user2 ...` — grant access to many at once\n"
        "• `!access remove @user` — revoke access\n"
        "• `!access map @user <sf_user_id>` — manually pin a Slack user to a Salesforce User ID\n"
        "• `!access unmap @user` — clear the SF user mapping"
    )
