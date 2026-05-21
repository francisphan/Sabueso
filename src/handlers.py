"""Message processing pipeline: Slack events -> agentic NLP loop -> response.

Write tools (currently sf_create_opportunity_for_person and sf_log_touch) halt
the agent loop and surface as Block Kit confirmation cards. The Bolt action
handler in slack_bot.py calls execute_pending() / cancel_pending() on click.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import permissions
import pending_ops
import sf_intents
from mcp_client import call_tool
from nlp import AgentResult, run_agent, build_history_from_agent_run
from permissions import check_access, parse_admin_command, can_use_tool
from pending_ops import PendingOp
from tools_catalog import INTENT_TOOLS

if TYPE_CHECKING:
    from slack_sdk import WebClient

log = logging.getLogger(__name__)

# Conversation history keyed by (user_id, channel, thread_ts).
_conversations: dict[tuple[str, str, str], list[dict]] = {}
_conversations_lock = threading.Lock()
_MAX_HISTORY = 20

_METRICS_PATH = Path(os.getenv("METRICS_FILE", ".cache/metrics.jsonl"))
_metrics_lock = threading.Lock()


def _convo_key(user_id: str, channel: str, thread_ts: str | None) -> tuple[str, str, str]:
    return (user_id, channel, thread_ts or "")


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
        admin_response = parse_admin_command(text, user_id)
        if admin_response is not None:
            say(text=admin_response, thread_ts=reply_ts)
            return

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

            if result.pending_confirmation is not None:
                _handle_pending(result, text, user_id, channel, thread_ts, say)
            else:
                _send_response(say, result.text, reply_ts)
                _append_history(key, text, result.text)
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
                event="agent_turn",
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


def _handle_pending(
    result: AgentResult,
    text: str,
    user_id: str,
    channel: str,
    thread_ts: str | None,
    say,
) -> None:
    """Halt of the agent loop: gate by permission, then post the confirmation card."""
    pc = result.pending_confirmation
    assert pc is not None  # guarded by caller

    if not can_use_tool(user_id, pc.tool_name):
        say(
            text=(
                f"That action (`{pc.tool_name}`) is only available to sales reps "
                "or admins. Ask an admin to upgrade your role if you need access."
            ),
            thread_ts=thread_ts,
        )
        return

    op = pending_ops.register(
        tool_name=pc.tool_name,
        arguments=pc.arguments,
        summary=pc.summary,
        requester_user_id=user_id,
        channel=channel,
        thread_ts=thread_ts,
        user_message=text,
    )
    blocks = _render_confirmation_card(op)
    say(text=pc.summary, blocks=blocks, thread_ts=thread_ts)


def _render_confirmation_card(op: PendingOp) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": op.summary},
        },
        {
            "type": "actions",
            "block_id": "sabueso_pending",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "action_id": "sabueso_confirm_op",
                    "value": op.action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "sabueso_cancel_op",
                    "value": op.action_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "_Expires in 15 minutes._"},
            ],
        },
    ]


# ── Confirmation handlers (called from slack_bot.py @app.action) ───────────

def execute_pending(action_id: str, body: dict, client: "WebClient") -> None:
    """Execute a pending op after the rep clicks Confirm."""
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    clicker = body["user"]["id"]

    # Re-check permissions at execute time: an admin may have demoted the
    # requester between card-post and click. peek so we can leave the op
    # alone if denial happens — pop only after authorization confirmed.
    op_preview = pending_ops.peek(action_id)
    if op_preview is not None and not can_use_tool(op_preview.requester_user_id, op_preview.tool_name):
        try:
            client.chat_postEphemeral(
                channel=channel,
                user=clicker,
                text=(
                    f"Permission revoked for `{op_preview.tool_name}` — this action cannot be confirmed."
                ),
            )
            client.chat_update(
                channel=channel,
                ts=message_ts,
                text="_Permission revoked. Cancelled._",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "_Permission revoked. Cancelled._"}},
                ],
            )
        except Exception:
            log.exception("Failed to notify permission revoke")
        # Drop the op so the card can't be reused.
        pending_ops.pop(action_id)
        return

    op, err = pending_ops.pop_if_authorized(action_id, clicker)
    if err == "expired":
        try:
            client.chat_update(
                channel=channel,
                ts=message_ts,
                text="_This action expired or was already taken._",
                blocks=[],
            )
        except Exception:
            log.exception("Failed to update expired card")
        return
    if err == "forbidden":
        try:
            client.chat_postEphemeral(
                channel=channel,
                user=clicker,
                text="Only the person who asked for this action can confirm it.",
            )
        except Exception:
            log.exception("Failed to post ephemeral denial")
        return
    assert op is not None  # narrowed by err check

    # Flip the card to a "working" state IMMEDIATELY so the buttons vanish
    # before SF round-trips complete (2-3 sec). Prevents double-confirm and
    # gives the rep visual acknowledgement that their click registered.
    try:
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=f"⏳ Working on it: {op.summary}",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"⏳ *Working on it…*\n{op.summary}"},
                },
            ],
        )
    except Exception:
        log.exception("Failed to show working state — proceeding")

    started = time.monotonic()
    error_type: str | None = None
    result_text = ""
    try:
        result_text = _execute_intent(op, client)
    except Exception as exc:
        log.exception("execute_pending failed for tool=%s", op.tool_name)
        error_type = type(exc).__name__
        result_text = (
            "Sorry, something went wrong executing that. The error has been "
            "logged for investigation."
        )

    # Replace the card with a status that reflects what actually happened.
    # An exception clearly failed; a non-ok status result (needs_person_details,
    # ambiguous_*, create_failed, etc.) is also a failure from the rep's POV.
    succeeded = error_type is None and result_text.startswith("✅")
    if succeeded:
        card_text = f"✓ Confirmed: {op.summary}"
        card_section = f"✓ *Confirmed*\n{op.summary}"
    else:
        card_text = f"⚠️ Failed: {op.summary}"
        card_section = f"⚠️ *Failed*\n{op.summary}"
    try:
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text=card_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": card_section},
                },
            ],
        )
    except Exception:
        log.exception("Failed to update card on confirm")

    # Post the result in the same thread.
    try:
        client.chat_postMessage(
            channel=op.channel,
            thread_ts=op.thread_ts,
            text=result_text,
        )
    except Exception:
        log.exception("Failed to post execute result")

    _append_history(
        _convo_key(op.requester_user_id, op.channel, op.thread_ts),
        op.user_message,
        result_text,
    )

    _record_metrics(
        user_id=op.requester_user_id,
        channel=op.channel,
        thread_ts=op.thread_ts,
        message_chars=len(op.user_message),
        duration_ms=int((time.monotonic() - started) * 1000),
        result=None,
        unhandled_error=error_type,
        event="write_confirmed",
        extras={"tool": op.tool_name, "action_id": op.action_id},
    )


def cancel_pending(action_id: str, body: dict, client: "WebClient") -> None:
    channel = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    clicker = body["user"]["id"]

    op, err = pending_ops.pop_if_authorized(action_id, clicker)
    if err == "expired":
        try:
            client.chat_update(
                channel=channel,
                ts=message_ts,
                text="_Action expired or already taken._",
                blocks=[],
            )
        except Exception:
            log.exception("Failed to update expired card on cancel")
        return
    if err == "forbidden":
        try:
            client.chat_postEphemeral(
                channel=channel,
                user=clicker,
                text="Only the person who asked for this action can cancel it.",
            )
        except Exception:
            log.exception("Failed to post ephemeral denial on cancel")
        return
    assert op is not None

    try:
        client.chat_update(
            channel=channel,
            ts=message_ts,
            text="_Cancelled._",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "_Cancelled._"}},
            ],
        )
    except Exception:
        log.exception("Failed to update card on cancel")

    _append_history(
        _convo_key(op.requester_user_id, op.channel, op.thread_ts),
        op.user_message,
        "(cancelled)",
    )

    _record_metrics(
        user_id=op.requester_user_id,
        channel=op.channel,
        thread_ts=op.thread_ts,
        message_chars=len(op.user_message),
        duration_ms=0,
        result=None,
        unhandled_error=None,
        event="write_cancelled",
        extras={"tool": op.tool_name, "action_id": op.action_id},
    )


# ── Intent execution + result formatting ────────────────────────────────────

def _execute_intent(op: PendingOp, client: "WebClient") -> str:
    if op.tool_name not in INTENT_TOOLS:
        raise NotImplementedError(f"No local executor for tool {op.tool_name!r}")

    fn_name = INTENT_TOOLS[op.tool_name]
    fn = getattr(sf_intents, fn_name)
    result = fn(
        slack_user_id=op.requester_user_id,
        slack_client=client,
        **op.arguments,
    )
    return _format_intent_result(op.tool_name, result)


def _format_intent_result(tool_name: str, result: dict) -> str:
    status = result.get("status")

    if tool_name == "sf_create_opportunity_for_person":
        if status == "ok":
            lines = [f"✅ Created opportunity for *{result.get('person_name', '?')}*"]
            url = result.get("opp_url")
            if url and url.startswith("http"):
                lines.append(f"🔗 <{url}|View in Salesforce>")
            elif url:
                lines.append(f"Opportunity ID: `{url}`")
            touches = result.get("touches", []) or []
            if touches:
                ok = [t.get("subject") for t in touches if t.get("ok")]
                bad = [t.get("subject") for t in touches if not t.get("ok")]
                if ok:
                    lines.append(f"📝 Logged touches: {', '.join(ok)}")
                if bad:
                    lines.append(
                        f"⚠️ Failed touches: {', '.join(bad)} — add them manually in SF"
                    )
            return "\n".join(lines)
        return _format_failure(status, result, "create the opportunity")

    if tool_name == "sf_log_touch":
        if status == "ok":
            url = result.get("opp_url")
            link = f"<{url}|opportunity>" if url and url.startswith("http") else "opportunity"
            return f"✅ Logged *{result.get('subject', 'touch')}* on {link}"
        return _format_failure(status, result, "log the touch")

    return f"Status: `{status}`\n```{json.dumps(result, indent=2, default=str)}```"


def _format_failure(status: str | None, result: dict, action: str) -> str:
    if status == "needs_person_details":
        return result.get(
            "message",
            "I don't see anyone matching. Can you give me their email or phone?",
        )
    if status == "ambiguous_person":
        candidates = result.get("candidates", []) or []
        lines = ["I see multiple matches — which one did you mean?"]
        for c in candidates:
            name = c.get("name") or c.get("Name") or "?"
            email = c.get("email") or c.get("Email") or "(no email)"
            lines.append(f"• *{name}* — {email}")
        return "\n".join(lines)
    if status == "no_sf_identity":
        return result.get(
            "message",
            "I can't find a Salesforce user matching your Slack email. "
            "Ask an admin to run `!access map @you <sf_user_id>`.",
        )
    if status == "invalid_product":
        expected = ", ".join(result.get("expected", []) or [])
        return f"Unknown product. Expected one of: {expected}."
    if status == "invalid_subject":
        return f"Unknown touch subject: `{result.get('subject', '?')}`."
    if status == "no_open_opps":
        return result.get("message", "No open opportunities found for that person.")
    if status == "ambiguous_opp":
        candidates = result.get("candidates", []) or []
        lines = ["Multiple open opportunities — which one?"]
        for c in candidates:
            name = c.get("name") or c.get("Name") or "?"
            stage = c.get("stage") or c.get("StageName") or "?"
            lines.append(f"• *{name}* — {stage}")
        return "\n".join(lines)
    if status == "create_failed":
        err = result.get("error", "")
        return f"Failed to {action}: {err}".strip()
    return f"Couldn't {action}: status `{status}`."


# ── Metrics + history helpers ───────────────────────────────────────────────

def _append_history(key: tuple[str, str, str], user_text: str, assistant_text: str) -> None:
    entries = build_history_from_agent_run(user_text, assistant_text)
    with _conversations_lock:
        hist = _conversations.setdefault(key, [])
        hist.extend(entries)
        if len(hist) > _MAX_HISTORY * 2:
            _conversations[key] = hist[-_MAX_HISTORY * 2:]


def _record_metrics(
    *,
    user_id: str,
    channel: str,
    thread_ts: str | None,
    message_chars: int,
    duration_ms: int,
    result: AgentResult | None,
    unhandled_error: str | None,
    event: str = "agent_turn",
    extras: dict | None = None,
) -> None:
    """Append one JSONL record per agent turn or write confirm/cancel."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
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
        "pending_confirmation": (
            result.pending_confirmation.tool_name
            if result and result.pending_confirmation
            else None
        ),
        "error_type": unhandled_error or (result.error_type if result else None),
    }
    if extras:
        record.update(extras)
    try:
        with _metrics_lock:
            _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _METRICS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        log.exception("Failed to write metrics record to %s", _METRICS_PATH)


def _send_response(say, text: str, thread_ts: str | None) -> None:
    """Send a response to Slack, chunking if it exceeds the character limit."""
    if len(text) <= _SLACK_MAX_CHARS:
        say(text=text, thread_ts=thread_ts)
        return

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
