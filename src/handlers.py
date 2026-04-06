"""Message processing pipeline: Slack events -> agentic NLP loop -> response."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp_client import call_tool
from nlp import run_agent, build_history_from_agent_run
from permissions import check_access, parse_admin_command

if TYPE_CHECKING:
    from slack_sdk import WebClient

log = logging.getLogger(__name__)

# Per-user conversation history keyed by Slack user ID.
_conversations: dict[str, list[dict]] = {}
_MAX_HISTORY = 20


# ── Public entry points (called by slack_bot.py) ────────────────────────────

def handle_direct_message(event: dict, say, client: "WebClient"):
    _process_message(
        text=event["text"],
        user_id=event["user"],
        channel=event["channel"],
        thread_ts=event.get("thread_ts"),
        say=say,
        client=client,
    )


def handle_mention(event: dict, say, client: "WebClient"):
    raw = event.get("text", "")
    text = raw.split(">", 1)[-1].strip() if "<@" in raw else raw

    _process_message(
        text=text,
        user_id=event["user"],
        channel=event["channel"],
        thread_ts=event.get("thread_ts"),
        say=say,
        client=client,
    )


# ── Core pipeline ───────────────────────────────────────────────────────────

def _process_message(
    text: str,
    user_id: str,
    channel: str,
    thread_ts: str | None,
    say,
    client: "WebClient",
):
    reply_ts = thread_ts or None

    # Admin commands bypass NLP
    admin_response = parse_admin_command(text, user_id)
    if admin_response is not None:
        say(text=admin_response, thread_ts=reply_ts)
        return

    # Access check
    denial = check_access(user_id)
    if denial:
        say(text=denial, thread_ts=reply_ts)
        return

    say(text="_Sniffing around..._", thread_ts=reply_ts)

    # Run the agentic loop
    history = _conversations.get(user_id, [])
    response = run_agent(
        message=text,
        tool_executor=call_tool,
        conversation_history=history or None,
    )

    say(text=response, thread_ts=reply_ts)

    # Update conversation history (compact: just user message + final answer)
    history_entries = build_history_from_agent_run(text, response)
    history = _conversations.setdefault(user_id, [])
    history.extend(history_entries)
    if len(history) > _MAX_HISTORY * 2:
        _conversations[user_id] = history[-_MAX_HISTORY * 2:]
