"""Per-turn tracing context, propagated from the agent loop to MCP calls.

Two pieces of context ride along every MCP tool call as request headers so the
agent-b MCP server can attribute the traffic:

- A fresh correlation ID, minted at the start of each agent run (one Slack
  turn), sent as ``X-Correlation-ID`` — ties the server's per-tool logs back to
  the conversation turn that caused them.
- The Slack end-user ID of the human who triggered the turn, sent as
  ``X-End-User`` — lets agent-b's usage analytics attribute bot traffic per
  person.

Both live in ContextVars so they're isolated per thread/async context (the bot
handles each DM on its own thread) and are set/reset at each turn boundary.

Named ``tracing`` rather than ``trace`` to avoid shadowing the stdlib ``trace``
module, since ``src/`` is on ``sys.path``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar(
    "sabueso_correlation_id", default=None
)
_end_user: ContextVar[str | None] = ContextVar("sabueso_end_user", default=None)


def new_turn_id() -> str:
    """Mint a short, unique correlation ID for a conversation turn."""
    return uuid.uuid4().hex[:12]


def set_correlation_id(cid: str | None) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_end_user(user_id: str | None) -> None:
    """Record the Slack user whose turn this is (None clears it)."""
    _end_user.set(user_id)


def get_end_user() -> str | None:
    return _end_user.get()
