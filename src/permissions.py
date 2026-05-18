"""
Role-based access control for Sabueso.

Two permission levels:
  - admin:     read + write access, can manage the access list
  - read_only: read-only access (queries, lookups, exports)

Users not in the access list are denied entirely.

The access list is stored in a JSON file so admins can manage it
via Slack commands without redeploying.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class Role(str, Enum):
    ADMIN = "admin"
    READ_ONLY = "read_only"


_ACL_PATH = Path(os.getenv("ACL_FILE", "acl.json"))
_DEFAULT_ADMIN = os.getenv("BOT_ADMIN_USER_ID", "U0ACKBHM2S1")
_acl_lock = threading.Lock()


def _load_acl() -> dict[str, str]:
    if _ACL_PATH.exists():
        try:
            data = json.loads(_ACL_PATH.read_text())
            return {uid: role for uid, role in data.items()}
        except (json.JSONDecodeError, AttributeError):
            log.warning("Corrupt ACL file at %s — falling back to defaults", _ACL_PATH)
            return {_DEFAULT_ADMIN: Role.ADMIN}

    # Fresh install (or fresh persistent volume): seed with the bootstrap admin
    # and persist immediately so the on-disk file becomes the source of truth.
    bootstrap = {_DEFAULT_ADMIN: Role.ADMIN.value}
    try:
        _save_acl(bootstrap)
        log.info("Bootstrapped ACL at %s with admin %s", _ACL_PATH, _DEFAULT_ADMIN)
    except OSError as exc:
        log.warning("Could not persist bootstrap ACL to %s (%s) — running in-memory", _ACL_PATH, exc)
    return bootstrap


def _save_acl(acl: dict[str, str]) -> None:
    _ACL_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ACL_PATH.write_text(json.dumps(acl, indent=2))
    log.info("ACL saved to %s", _ACL_PATH)


def get_role(user_id: str) -> Role | None:
    with _acl_lock:
        acl = _load_acl()
    role_str = acl.get(user_id)
    if role_str is None:
        return None
    try:
        return Role(role_str)
    except ValueError:
        log.warning("Unknown role %r for user %s", role_str, user_id)
        return None


def is_authorized(user_id: str) -> bool:
    return get_role(user_id) is not None


def is_admin(user_id: str) -> bool:
    return get_role(user_id) == Role.ADMIN


def can_write(user_id: str) -> bool:
    return get_role(user_id) == Role.ADMIN


def check_access(user_id: str, is_write_operation: bool = False) -> str | None:
    role = get_role(user_id)

    if role is None:
        return (
            "Sorry, I don't have you on my list. "
            "Ask an admin to grant you access and I'll start sniffing things out for you."
        )

    if is_write_operation and role != Role.ADMIN:
        return (
            "You have read-only access — I can fetch data for you, but I can't "
            "make changes. Ask an admin to upgrade your permissions if you need write access."
        )

    return None


def add_user(user_id: str, role: Role) -> str:
    with _acl_lock:
        acl = _load_acl()
        was_existing = user_id in acl
        acl[user_id] = role.value
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
            if uid in acl:
                updated.append(uid)
            else:
                added.append(uid)
            acl[uid] = role.value
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


def list_users() -> str:
    with _acl_lock:
        acl = _load_acl()
    if not acl:
        return "No users in the access list."
    lines = []
    for uid, role in sorted(acl.items(), key=lambda x: x[1]):
        lines.append(f"• <@{uid}> — `{role}`")
    return "*Authorized users:*\n" + "\n".join(lines)


def _parse_user_mention(token: str) -> str:
    """Extract user ID from a `<@U12345>` or `<@U12345|name>` token."""
    return token.strip("<@>").split("|")[0]


def parse_admin_command(text: str, requesting_user_id: str) -> str | None:
    """Parse admin commands from message text.

    Supported:
        !access list
        !access add <@U12345> read_only|admin
        !access bulk-add read_only|admin <@U1> <@U2> ...
        !access remove <@U12345>
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
            return f"Unknown role `{role_str}`. Use `admin` or `read_only`."
        return add_user(target, role)

    if cmd == "bulk-add" and len(parts) >= 4:
        role_str = parts[2].lower()
        try:
            role = Role(role_str)
        except ValueError:
            return f"Unknown role `{role_str}`. Use `admin` or `read_only`."
        user_ids = [_parse_user_mention(p) for p in parts[3:]]
        return bulk_add_users(user_ids, role)

    if cmd == "remove" and len(parts) >= 3:
        target = _parse_user_mention(parts[2])
        return remove_user(target)

    return _admin_help()


def _admin_help() -> str:
    return (
        "*Access management commands:*\n"
        "• `!access list` — show authorized users\n"
        "• `!access add @user admin` — grant read+write access\n"
        "• `!access add @user read_only` — grant read-only access\n"
        "• `!access bulk-add read_only @user1 @user2 ...` — grant access to many at once\n"
        "• `!access remove @user` — revoke access"
    )
