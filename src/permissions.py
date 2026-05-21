"""
Role-based access control for Sabueso.

Roles:
  - admin:     full read + write; can manage the access list.
  - sales_rep: read + a narrow write scope (opportunity + touch intent tools).
  - read_only: read-only.

A user can hold MULTIPLE roles — their effective tool scope is the union
of every role's base set, then per-user `extra`/`deny` lists applied on top.

Users not in the access list are denied entirely.

The ACL is stored as JSON. Three entry shapes are supported, all
backwards-compatible:

    "U123": "admin"                              # bare string — single role
    "U456": {"role": "sales_rep"}                # singular `role` key — single role
    "U789": {"roles": ["sales_rep", "ops_admin"],# plural `roles` key — multi-role
             "extra": ["sf_log_touch"],
             "deny": [],
             "sf_user_id": "005xx0000012345"}

The bare-string / single-role compact forms are used on serialization when
the user has exactly one role and no other overrides, so existing ACL files
stay valid after the upgrade.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    roles: list[Role]
    extra: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    sf_user_id: str | None = None

    def is_compact(self) -> bool:
        """True if a single-role string form fully captures this entry."""
        return (
            len(self.roles) == 1
            and not self.extra
            and not self.deny
            and self.sf_user_id is None
        )

    def has_role(self, role: Role) -> bool:
        return role in self.roles


_ACL_PATH = Path(os.getenv("ACL_FILE", "acl.json"))
_DEFAULT_ADMIN = os.getenv("BOT_ADMIN_USER_ID", "U0ACKBHM2S1")
_acl_lock = threading.Lock()


# ── Load / save ─────────────────────────────────────────────────────────────

def _parse_role_list(raw: object) -> list[Role] | None:
    """Convert a single role-name or list of names into a list of Role enums."""
    if isinstance(raw, str):
        try:
            return [Role(raw)]
        except ValueError:
            log.warning("Unknown role string %r in ACL", raw)
            return None
    if isinstance(raw, list):
        out: list[Role] = []
        seen: set[Role] = set()
        for item in raw:
            if not isinstance(item, str):
                log.warning("Skipping non-string role entry %r", item)
                continue
            try:
                role = Role(item)
            except ValueError:
                log.warning("Skipping unknown role %r in role list", item)
                continue
            if role in seen:
                continue
            seen.add(role)
            out.append(role)
        return out or None
    return None


def _parse_entry(raw: object) -> AclEntry | None:
    """Normalize one ACL value (string, single-role dict, or multi-role dict)."""
    if isinstance(raw, str):
        roles = _parse_role_list(raw)
        if roles is None:
            return None
        return AclEntry(roles=roles)

    if isinstance(raw, dict):
        # Accept both `roles: [...]` and the legacy `role: "..."` shapes.
        if "roles" in raw:
            roles = _parse_role_list(raw["roles"])
        elif "role" in raw:
            roles = _parse_role_list(raw["role"])
        else:
            log.warning("ACL object missing role/roles key: %r", raw)
            return None
        if roles is None:
            return None
        return AclEntry(
            roles=roles,
            extra=list(raw.get("extra", [])),
            deny=list(raw.get("deny", [])),
            sf_user_id=raw.get("sf_user_id") or None,
        )

    log.warning("Unsupported ACL value type %r", type(raw))
    return None


def _serialize_entry(entry: AclEntry) -> str | dict:
    # Compact: a single role and no overrides → bare string.
    if entry.is_compact():
        return entry.roles[0].value
    out: dict = {}
    if len(entry.roles) == 1:
        out["role"] = entry.roles[0].value
    else:
        out["roles"] = [r.value for r in entry.roles]
    if entry.extra:
        out["extra"] = entry.extra
    if entry.deny:
        out["deny"] = entry.deny
    if entry.sf_user_id:
        out["sf_user_id"] = entry.sf_user_id
    return out


def _bootstrap_acl() -> dict[str, AclEntry]:
    return {_DEFAULT_ADMIN: AclEntry(roles=[Role.ADMIN])}


def _load_acl() -> dict[str, AclEntry]:
    if _ACL_PATH.exists():
        try:
            raw = json.loads(_ACL_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt ACL file at %s — falling back to defaults", _ACL_PATH)
            return _bootstrap_acl()
        if not isinstance(raw, dict):
            log.warning(
                "ACL file at %s has wrong shape (got %s, expected object) — falling back to defaults",
                _ACL_PATH, type(raw).__name__,
            )
            return _bootstrap_acl()
        entries: dict[str, AclEntry] = {}
        for uid, value in raw.items():
            parsed = _parse_entry(value)
            if parsed is not None:
                entries[uid] = parsed
        return entries

    # Fresh install: seed the bootstrap admin and persist.
    bootstrap = _bootstrap_acl()
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


def get_roles(user_id: str) -> list[Role]:
    """Return all roles assigned to this user (empty list if unknown)."""
    entry = _get_entry(user_id)
    return list(entry.roles) if entry else []


def get_role(user_id: str) -> Role | None:
    """Return the user's first / primary role (None if unknown).

    Kept for backwards compatibility. New code should call get_roles().
    """
    entry = _get_entry(user_id)
    return entry.roles[0] if entry and entry.roles else None


def is_authorized(user_id: str) -> bool:
    return _get_entry(user_id) is not None


def is_admin(user_id: str) -> bool:
    """True if any of the user's roles is admin."""
    entry = _get_entry(user_id)
    return entry is not None and Role.ADMIN in entry.roles


def get_sf_user_override(user_id: str) -> str | None:
    """Return the manually-mapped SF User ID for this Slack user, if any."""
    entry = _get_entry(user_id)
    return entry.sf_user_id if entry else None


def can_use_tool(user_id: str, tool_name: str) -> bool:
    """Whether this user is authorized to invoke a given tool.

    Effective scope = union(role base scopes) ∪ extra − deny.
    Admin in ANY role short-circuits to True.
    """
    entry = _get_entry(user_id)
    if entry is None:
        return False
    if Role.ADMIN in entry.roles:
        return True
    base: set[str] = set()
    for role in entry.roles:
        base |= ROLE_TOOL_SCOPES.get(role, set())
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

def _dedupe_roles(roles: list[Role]) -> list[Role]:
    """Preserve first-seen order, drop duplicates."""
    seen: set[Role] = set()
    out: list[Role] = []
    for r in roles:
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def set_user_roles(user_id: str, roles: list[Role]) -> str:
    """Replace a user's role list. Preserves overrides (extra/deny/sf_user_id)."""
    if not roles:
        return "At least one role is required."
    deduped = _dedupe_roles(roles)
    with _acl_lock:
        acl = _load_acl()
        existing = acl.get(user_id)
        was_existing = existing is not None
        if existing is not None:
            existing.roles = deduped
        else:
            acl[user_id] = AclEntry(roles=deduped)
        _save_acl(acl)
    role_str = ", ".join(f"*{r.value}*" for r in deduped)
    action = "Updated" if was_existing else "Added"
    return f"{action} <@{user_id}> with {role_str} access."


def add_user(user_id: str, role: Role) -> str:
    """Set a user to a single role. Convenience wrapper around set_user_roles."""
    return set_user_roles(user_id, [role])


def add_role(user_id: str, role: Role) -> str:
    """Append a role to a user's role list (no-op if already present)."""
    with _acl_lock:
        acl = _load_acl()
        existing = acl.get(user_id)
        if existing is None:
            return f"<@{user_id}> is not in the access list. Use `!access add` first."
        if role in existing.roles:
            return f"<@{user_id}> already has *{role.value}*."
        existing.roles = _dedupe_roles(existing.roles + [role])
        _save_acl(acl)
    return f"Added *{role.value}* to <@{user_id}> — now has {', '.join(f'`{r.value}`' for r in existing.roles)}."


def remove_role(user_id: str, role: Role) -> str:
    """Remove a role from a user's role list. Refuses to remove the last role."""
    with _acl_lock:
        acl = _load_acl()
        existing = acl.get(user_id)
        if existing is None:
            return f"<@{user_id}> is not in the access list."
        if role not in existing.roles:
            return f"<@{user_id}> doesn't have *{role.value}*."
        if len(existing.roles) == 1:
            return (
                f"Cannot remove the last role from <@{user_id}>. "
                "Use `!access remove` to revoke access entirely."
            )
        existing.roles = [r for r in existing.roles if r != role]
        _save_acl(acl)
    return f"Removed *{role.value}* from <@{user_id}> — now has {', '.join(f'`{r.value}`' for r in existing.roles)}."


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
                existing.roles = [role]
                updated.append(uid)
            else:
                acl[uid] = AclEntry(roles=[role])
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
    # Sort by the user's primary role name, then by user id for stability.
    for uid, entry in sorted(acl.items(), key=lambda x: (x[1].roles[0].value, x[0])):
        role_str = ", ".join(f"`{r.value}`" for r in entry.roles)
        annotations: list[str] = []
        if entry.sf_user_id:
            annotations.append(f"SF: `{entry.sf_user_id}`")
        if entry.extra:
            annotations.append("extra: " + ", ".join(f"`{t}`" for t in entry.extra))
        if entry.deny:
            annotations.append("deny: " + ", ".join(f"`{t}`" for t in entry.deny))
        suffix = " (" + "; ".join(annotations) + ")" if annotations else ""
        lines.append(f"• <@{uid}> — {role_str}{suffix}")
    return "*Authorized users:*\n" + "\n".join(lines)


# ── Admin command parser ────────────────────────────────────────────────────

def _parse_user_mention(token: str) -> str:
    """Extract user ID from a `<@U12345>` or `<@U12345|name>` token."""
    return token.strip("<@>").split("|")[0]


def _parse_role_token(token: str) -> tuple[Role | None, str | None]:
    """Return (role, None) on success, (None, error_message) on failure.

    Note: Role inherits from str, so an isinstance(value, str) discriminator
    doesn't work — hence the explicit tuple.
    """
    try:
        return Role(token.lower()), None
    except ValueError:
        return None, f"Unknown role `{token}`. Use `admin`, `sales_rep`, or `read_only`."


def _parse_role_list_token(token: str) -> tuple[list[Role] | None, str | None]:
    """Parse a role list, tolerant of comma- and/or whitespace-separated input.

    Accepts: `sales_rep,read_only`, `sales_rep, read_only`, `sales_rep read_only`.
    Returns (roles, None) on success, (None, error_msg) on any unknown role.
    """
    roles: list[Role] = []
    for piece in re.split(r"[,\s]+", token):
        piece = piece.strip()
        if not piece:
            continue
        try:
            roles.append(Role(piece.lower()))
        except ValueError:
            return None, f"Unknown role `{piece}`. Use `admin`, `sales_rep`, or `read_only`."
    if not roles:
        return None, "At least one role is required."
    return roles, None


def parse_admin_command(text: str, requesting_user_id: str) -> str | None:
    """Parse admin commands from message text.

    Supported:
        !access list
        !access add <@U12345> role1[,role2,...]
        !access add-role <@U12345> <role>
        !access remove-role <@U12345> <role>
        !access bulk-add <role> <@U1> <@U2> ...
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
        # Rejoin everything after the user so `sales_rep, read_only` works
        # the same as `sales_rep,read_only`.
        roles, err = _parse_role_list_token(" ".join(parts[3:]))
        if err:
            return err
        assert roles is not None
        return set_user_roles(target, roles)

    if cmd == "add-role" and len(parts) >= 4:
        target = _parse_user_mention(parts[2])
        role, err = _parse_role_token(parts[3])
        if err:
            return err
        assert role is not None
        return add_role(target, role)

    if cmd == "remove-role" and len(parts) >= 4:
        target = _parse_user_mention(parts[2])
        role, err = _parse_role_token(parts[3])
        if err:
            return err
        assert role is not None
        return remove_role(target, role)

    if cmd == "bulk-add" and len(parts) >= 4:
        role, err = _parse_role_token(parts[2])
        if err:
            return err
        assert role is not None
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
        "• `!access add @user role[,role2,...]` — grant access (one or more roles)\n"
        "• `!access add-role @user role` — add a role to an existing user\n"
        "• `!access remove-role @user role` — drop one of a user's roles (refuses the last one)\n"
        "• `!access bulk-add role @user1 @user2 ...` — grant the same role to many at once\n"
        "• `!access remove @user` — revoke access entirely\n"
        "• `!access map @user <sf_user_id>` — manually pin a Slack user to a Salesforce User ID\n"
        "• `!access unmap @user` — clear the SF user mapping\n"
        "_Roles: admin, sales_rep, read_only._"
    )
