"""Admin alerting for denied bot-access attempts.

When a Slack user who is not on the ACL messages the bot, the denial reply is
user-facing only — nothing tells an admin that someone knocked. This module
tracks denied attempts per user and decides when an admin notice is due:
immediately on a user's first attempt, then at most once per cooldown window,
reporting how many further attempts were made in between. State is in-memory;
a restart just means one extra notice, which is harmless.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import permissions
from nlp import _escape_mrkdwn

_COOLDOWN_SECONDS = float(os.getenv("ACCESS_ALERT_COOLDOWN_SECONDS", "3600"))


@dataclass
class _DeniedRecord:
    attempts_since_notice: int = 0
    last_notice: float | None = None


_records: dict[str, _DeniedRecord] = {}
_lock = threading.Lock()


def record_denied_attempt(user_id: str, now: float | None = None) -> int | None:
    """Register one denied attempt from *user_id*.

    Returns the attempt count to report (>= 1) when an admin notice is due,
    or None while the user is inside the cooldown window since the last
    notice about them.
    """
    now = time.monotonic() if now is None else now
    with _lock:
        rec = _records.setdefault(user_id, _DeniedRecord())
        rec.attempts_since_notice += 1
        if rec.last_notice is not None and now - rec.last_notice < _COOLDOWN_SECONDS:
            return None
        count = rec.attempts_since_notice
        rec.last_notice = now
        rec.attempts_since_notice = 0
        return count


def alert_recipients() -> list[str]:
    """Slack user IDs to DM about denied attempts.

    ``ACCESS_ALERT_USER_IDS`` (comma-separated) overrides; otherwise the
    bootstrap admin gets the notices.
    """
    raw = os.getenv("ACCESS_ALERT_USER_IDS", "")
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return ids or [permissions._DEFAULT_ADMIN]


def build_notice(user_id: str, attempts: int, display_name: str | None = None) -> str:
    # The denied user picks their own display name — escape it so a crafted
    # name can't ping anyone or splice fake instructions into the notice.
    name = _escape_mrkdwn(display_name) if display_name else None
    who = f"<@{user_id}>" + (f" ({name})" if name else "")
    if attempts == 1:
        lead = f":no_entry: {who} tried to use Sabueso but isn't on the access list."
    else:
        lead = (
            f":no_entry: {who} has tried to use Sabueso {attempts} times "
            "since the last notice and isn't on the access list."
        )
    return (
        f"{lead}\n"
        f"Reply here with `!access add <@{user_id}> read_only` to let them in, "
        "or ignore this to keep them out."
    )
