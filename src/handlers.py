"""Message processing pipeline: Slack events -> NLP -> MCP server (HTTP) -> response."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from export import generate_csv, generate_pdf, upload_file_to_thread
from mcp_client import call_tool
from nlp import parse_request, format_response, build_tool_result_messages
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
        thread_ts=event.get("thread_ts", event["ts"]),
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
        thread_ts=event.get("thread_ts", event["ts"]),
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

    # NLP intent parsing
    history = _conversations.get(user_id, [])
    intent = parse_request(text, conversation_history=history or None)
    log.info("Intent for user=%s: tool=%s params=%s", user_id, intent.tool_name, intent.parameters)

    # Clarification needed
    if intent.clarification_needed:
        say(text=intent.clarification_needed, thread_ts=reply_ts)
        return

    # Plain text response (greeting, etc.)
    if intent.tool_name is None:
        reply = intent.raw_text_response or "I'm not sure how to help with that. Could you rephrase?"
        say(text=reply, thread_ts=reply_ts)
        return

    # Write-operation permission check
    if intent.is_write_operation:
        write_denial = check_access(user_id, is_write_operation=True)
        if write_denial:
            say(text=write_denial, thread_ts=reply_ts)
            return

    # Execute via MCP server HTTP call
    say(text="_Sniffing around..._", thread_ts=reply_ts)

    try:
        result = call_tool(intent.tool_name, intent.parameters)
    except Exception as e:
        log.error("MCP call %s failed: %s", intent.tool_name, e, exc_info=True)
        say(text=f"Hit a wall on that one: `{e}`", thread_ts=reply_ts)
        return

    # Format response and reply
    formatted = format_response(result, text)
    say(text=formatted, thread_ts=reply_ts)

    # Update conversation history
    history_entries = build_tool_result_messages(intent, result)
    history = _conversations.setdefault(user_id, [])
    history.extend(history_entries)
    if len(history) > _MAX_HISTORY * 2:
        _conversations[user_id] = history[-_MAX_HISTORY * 2:]
