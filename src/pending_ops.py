"""
In-memory store of pending write operations awaiting Slack-button confirmation.

When the agent loop hits a write tool, it stashes a PendingOp here and returns
a Block Kit confirmation card. The Bolt action handler pops the op on Confirm
and executes it. Entries expire after a TTL so stale cards can't fire late.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TTL_SECONDS = 15 * 60


@dataclass
class PendingOp:
    action_id: str
    tool_name: str
    arguments: dict
    summary: str
    requester_user_id: str
    channel: str
    thread_ts: str | None
    expires_at: float
    # Original user text that triggered this pending op — used to update
    # conversation history after confirm/cancel.
    user_message: str = ""


_pending: dict[str, PendingOp] = {}
_lock = threading.Lock()


def register(
    *,
    tool_name: str,
    arguments: dict,
    summary: str,
    requester_user_id: str,
    channel: str,
    thread_ts: str | None,
    user_message: str = "",
    ttl_seconds: int = _TTL_SECONDS,
) -> PendingOp:
    action_id = uuid.uuid4().hex
    op = PendingOp(
        action_id=action_id,
        tool_name=tool_name,
        arguments=arguments,
        summary=summary,
        requester_user_id=requester_user_id,
        channel=channel,
        thread_ts=thread_ts,
        expires_at=time.time() + ttl_seconds,
        user_message=user_message,
    )
    with _lock:
        _purge_expired_locked()
        _pending[action_id] = op
    return op


def pop(action_id: str) -> PendingOp | None:
    """Remove and return a pending op. Returns None if missing or expired."""
    with _lock:
        _purge_expired_locked()
        op = _pending.pop(action_id, None)
    if op is None:
        return None
    if op.expires_at < time.time():
        return None
    return op


def pop_if_authorized(
    action_id: str, clicker_user_id: str
) -> tuple[PendingOp | None, str | None]:
    """Atomic pop gated by requester match.

    Returns:
        (op, None) on success — op was popped and is ready to execute.
        (None, "expired") if the op is missing or its TTL has elapsed.
        (None, "forbidden") if the clicker is not the originator — the op
            is left in place so the rightful user can still confirm.
    """
    with _lock:
        _purge_expired_locked()
        op = _pending.get(action_id)
        if op is None:
            return (None, "expired")
        if op.expires_at < time.time():
            del _pending[action_id]
            return (None, "expired")
        if clicker_user_id != op.requester_user_id:
            return (None, "forbidden")
        del _pending[action_id]
        return (op, None)


def _purge_expired_locked() -> None:
    now = time.time()
    expired = [k for k, v in _pending.items() if v.expires_at < now]
    for k in expired:
        del _pending[k]
    if expired:
        log.info("Purged %d expired pending op(s)", len(expired))


def peek(action_id: str) -> PendingOp | None:
    """Non-destructive lookup, for tests / debugging."""
    with _lock:
        return _pending.get(action_id)
