"""Message processing pipeline: Slack events -> agentic NLP loop -> response."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from mcp_client import call_tool
from nlp import AgentResult, run_agent, build_history_from_agent_run
from permissions import check_access, parse_admin_command

if TYPE_CHECKING:
    from slack_sdk import WebClient

log = logging.getLogger(__name__)

# Conversation history keyed by (user_id, channel, thread_ts).
# Isolates DMs from channel @mentions, and each channel thread from the next,
# so private context doesn't bleed into public replies for the same user.
_conversations: dict[tuple[str, str, str], list[dict]] = {}
_conversations_lock = threading.Lock()
_MAX_HISTORY = 20

_METRICS_PATH = Path(os.getenv("METRICS_FILE", ".cache/metrics.jsonl"))
_metrics_lock = threading.Lock()


def _convo_key(user_id: str, channel: str, thread_ts: str | None) -> tuple[str, str, str]:
    return (user_id, channel, thread_ts or "")

# Slack message character limit (actual is 40k, leave margin).
_SLACK_MAX_CHARS = 39_000


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

    try:
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

        key = _convo_key(user_id, channel, thread_ts)

        with _conversations_lock:
            history = list(_conversations.get(key, []))

        started = time.monotonic()
        result: AgentResult | None = None
        unhandled_error: str | None = None
        try:
            result = run_agent(
                message=text,
                tool_executor=call_tool,
                conversation_history=history or None,
            )
            _send_response(say, result.text, reply_ts)

            history_entries = build_history_from_agent_run(text, result.text)
            with _conversations_lock:
                hist = _conversations.setdefault(key, [])
                hist.extend(history_entries)
                if len(hist) > _MAX_HISTORY * 2:
                    _conversations[key] = hist[-_MAX_HISTORY * 2:]
        except Exception as exc:
            unhandled_error = type(exc).__name__
            raise
        finally:
            _record_metrics(
                user_id=user_id,
                channel=channel,
                thread_ts=thread_ts,
                message_chars=len(text),
                duration_ms=int((time.monotonic() - started) * 1000),
                result=result,
                unhandled_error=unhandled_error,
            )

    except Exception:
        log.exception("Unhandled error processing message from user=%s", user_id)
        try:
            say(
                text="Sorry, something went wrong on my end. Please try again.",
                thread_ts=reply_ts,
            )
        except Exception:
            log.exception("Failed to send error message to user=%s", user_id)


def _record_metrics(
    *,
    user_id: str,
    channel: str,
    thread_ts: str | None,
    message_chars: int,
    duration_ms: int,
    result: AgentResult | None,
    unhandled_error: str | None,
) -> None:
    """Append one JSONL record per turn for offline analysis."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_id": user_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "message_chars": message_chars,
        "duration_ms": duration_ms,
        "success": unhandled_error is None and (result is None or result.error_type is None),
        "steps": result.steps if result else 0,
        "tool_calls": result.tool_calls if result else [],
        "response_chars": len(result.text) if result else 0,
        "input_tokens": result.input_tokens if result else 0,
        "output_tokens": result.output_tokens if result else 0,
        "cache_read_tokens": result.cache_read_tokens if result else 0,
        "cache_creation_tokens": result.cache_creation_tokens if result else 0,
        "max_steps_hit": result.max_steps_hit if result else False,
        "blocked_writes": result.blocked_writes if result else 0,
        "error_type": unhandled_error or (result.error_type if result else None),
    }
    try:
        with _metrics_lock:
            _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _METRICS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
    except OSError:
        log.exception("Failed to write metrics record to %s", _METRICS_PATH)


def _send_response(say, text: str, thread_ts: str | None) -> None:
    """Send a response to Slack, chunking if it exceeds the character limit."""
    if len(text) <= _SLACK_MAX_CHARS:
        say(text=text, thread_ts=thread_ts)
        return

    # Split on double-newlines (paragraph boundaries) to keep chunks readable
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > _SLACK_MAX_CHARS and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        suffix = f"\n_({i + 1}/{len(chunks)})_" if len(chunks) > 1 else ""
        say(text=chunk + suffix, thread_ts=thread_ts)
