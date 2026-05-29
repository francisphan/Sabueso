"""Message processing pipeline: Slack events -> agentic NLP loop -> response.

Write tools (currently sf_create_opportunity_for_person and sf_log_touch) halt
the agent loop and surface as Block Kit confirmation cards. The Bolt action
handler in slack_bot.py calls execute_pending() / cancel_pending() on click.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import conversation_store
import permissions
import pending_ops
import sf_intents
from mcp_client import call_tool
from nlp import AgentResult, run_agent, build_history_from_agent_run, _escape_mrkdwn
from permissions import check_access, parse_admin_command, can_use_tool
from pending_ops import PendingOp
from tools_catalog import INTENT_TOOLS
from wine_enrichment import research_wine_sync

if TYPE_CHECKING:
    from slack_sdk import WebClient

log = logging.getLogger(__name__)


def _execute_tool(tool_name: str, arguments: dict):
    """Dispatch a tool call from the agent loop.

    ``wine_research`` runs locally — Agent B has no web access, so public wine
    research is done here via Gemini + Google Search grounding. Every other tool
    is delegated to the Agent B MCP server.
    """
    if tool_name == "wine_research":
        result = research_wine_sync(
            brand=arguments.get("brand", ""),
            owner_name=arguments.get("owner_name"),
            vintage=arguments.get("vintage"),
        )
        return result or {
            "found": False,
            "summary": "No public information found for this wine.",
        }
    return call_tool(tool_name, arguments)

# Conversation history (keyed by user/channel/thread) is persisted by
# conversation_store so it survives restarts; see that module.

_METRICS_PATH = Path(os.getenv("METRICS_FILE", ".cache/metrics.jsonl"))
_metrics_lock = threading.Lock()

# When on, the raw human request text is embedded in each metrics record (for
# mining real requests). Same flag that surfaces payloads in nlp.py's logs.
# Off by default so guest PII stays out of the metrics file.
_LOG_PAYLOADS = os.getenv("SABUESO_DEBUG_PAYLOADS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Short-lived set of processed Slack event keys. Slack re-delivers an event if
# our listener is slow (e.g. during an image download); without this guard the
# same message gets processed twice — double answers, or two independently
# confirmable write cards for one request.
_SEEN_TTL_SECONDS = 600
_seen_events: dict[str, float] = {}
_seen_lock = threading.Lock()


def _already_processed(key: str) -> bool:
    """Record *key* and report whether it had already been seen (TTL-pruned)."""
    now = time.monotonic()
    with _seen_lock:
        for k in [k for k, t in _seen_events.items() if now - t > _SEEN_TTL_SECONDS]:
            del _seen_events[k]
        if key in _seen_events:
            return True
        _seen_events[key] = now
        return False


def _convo_key(user_id: str, channel: str, thread_ts: str | None) -> tuple[str, str, str]:
    return (user_id, channel, thread_ts or "")


_SLACK_MAX_CHARS = 39_000

# Image attachments (e.g. a sommelier's wine-label photo) are passed to Claude
# as vision blocks. Anthropic accepts jpeg/png/gif/webp; cap size/count so a
# stray large upload can't blow the request size or token budget.
_VISION_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_IMAGES = 3
# Only ever send the bot token to Slack's own hosts. Slack serves private files
# from files.slack.com / *.slack.com / *.slack-edge.com.
_SLACK_HOST_SUFFIXES = (".slack.com", ".slack-edge.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a credentialed file fetch can't be bounced to an
    attacker-controlled or internal host (the bot token rides in the header)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, f"redirect blocked: {newurl}", headers, fp)


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def _is_slack_host(url: str) -> bool:
    """True only for https URLs served by a Slack-owned host."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "slack.com" or host.endswith(_SLACK_HOST_SUFFIXES)
    )


def _extract_images(event: dict, client: "WebClient") -> list[dict]:
    """Download image attachments from a Slack event into Claude vision blocks.

    Slack hosts uploads behind an authenticated URL, so we fetch the private
    download URL with the bot token. To avoid SSRF / token leakage we only send
    the credential to Slack-owned https hosts and refuse redirects. Non-images,
    non-Slack URLs, oversized files, and download failures are skipped (logged,
    not fatal).
    """
    files = event.get("files") or []
    if not files:
        return []

    token = getattr(client, "token", None) or os.environ.get("SLACK_BOT_TOKEN")
    images: list[dict] = []
    for f in files:
        if len(images) >= _MAX_IMAGES:
            break
        media_type = (f.get("mimetype") or "").lower()
        if media_type not in _VISION_MEDIA_TYPES:
            continue
        if (f.get("size") or 0) > _MAX_IMAGE_BYTES:
            log.warning("Skipping oversized image %s (%s bytes)", f.get("name"), f.get("size"))
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        if not _is_slack_host(url):
            log.warning("Skipping image %s — non-Slack URL host", f.get("name"))
            continue
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with _no_redirect_opener.open(req, timeout=10) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
            if len(data) > _MAX_IMAGE_BYTES:
                log.warning("Skipping image %s — exceeded size cap mid-download", f.get("name"))
                continue
            images.append(
                {
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode("ascii"),
                }
            )
        except Exception:
            log.exception("Failed to download Slack image %s", f.get("name"))
    return images


# ── Public entry points (called by slack_bot.py) ────────────────────────────

def handle_direct_message(event: dict, say, client: "WebClient"):
    _process_message(
        text=event.get("text", ""),
        user_id=event["user"],
        channel=event["channel"],
        thread_ts=event.get("thread_ts"),
        say=say,
        client=client,
        event=event,
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
        event=event,
    )


# ── Core pipeline ───────────────────────────────────────────────────────────

def _process_message(
    text: str,
    user_id: str,
    channel: str,
    thread_ts: str | None,
    say,
    client: "WebClient",
    event: dict | None = None,
):
    event = event or {}
    reply_ts = thread_ts or None

    # Drop Slack re-deliveries before doing any work, so a slow turn that trips a
    # retry isn't answered (or write-confirmed) twice. Keyed on the stable
    # client_msg_id, falling back to channel+ts.
    dedup_key = event.get("client_msg_id") or f"{channel}:{event.get('ts')}"
    if event and _already_processed(dedup_key):
        log.info("Skipping duplicate Slack event %s", dedup_key)
        return

    try:
        # Image-only messages have no text — skip the admin-command parse, which
        # only ever matches the leading "!" command syntax anyway.
        if text.strip():
            admin_response = parse_admin_command(text, user_id)
            if admin_response is not None:
                say(text=admin_response, thread_ts=reply_ts)
                return

        denial = check_access(user_id)
        if denial:
            say(text=denial, thread_ts=reply_ts)
            return

        say(text="_Sniffing around..._", thread_ts=reply_ts)

        # Download image attachments AFTER acking: the user gets immediate
        # feedback and the listener isn't blocked on network I/O up front.
        images = _extract_images(event, client)

        key = _convo_key(user_id, channel, thread_ts)
        history = conversation_store.get_history(key)

        # History is stored text-only; record a placeholder when the turn was
        # just an image so follow-up context stays coherent and non-empty.
        history_text = text if text.strip() else "[sent an image]"

        started = time.monotonic()
        result: AgentResult | None = None
        unhandled_error: str | None = None
        try:
            result = run_agent(
                message=text,
                tool_executor=_execute_tool,
                conversation_history=history or None,
                images=images,
            )

            if result.pending_confirmation is not None:
                _handle_pending(result, history_text, user_id, channel, thread_ts, say)
            else:
                _send_response(say, result.text, reply_ts)
                _append_history(key, history_text, result.text)
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
                message_text=text,
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
        message_text=op.user_message,
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
        message_text=op.user_message,
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
            lines = [f"✅ Created opportunity for *{_escape_mrkdwn(result.get('person_name', '?'))}*"]
            url = result.get("opp_url")
            if url and url.startswith("http"):
                lines.append(f"🔗 <{url}|View in Salesforce>")
            elif url:
                lines.append(f"Opportunity ID: `{url}`")
            touches = result.get("touches", []) or []
            if touches:
                ok = [_escape_mrkdwn(t.get("subject")) for t in touches if t.get("ok")]
                bad = [_escape_mrkdwn(t.get("subject")) for t in touches if not t.get("ok")]
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
            return f"✅ Logged *{_escape_mrkdwn(result.get('subject', 'touch'))}* on {link}"
        return _format_failure(status, result, "log the touch")

    return f"Status: `{status}`\n```{json.dumps(result, indent=2, default=str)}```"


def _format_failure(status: str | None, result: dict, action: str) -> str:
    # All record-derived / free-text values below are rendered into Slack mrkdwn,
    # so they pass through _escape_mrkdwn to defang pings (<!channel>) and link
    # spoofing (<http://evil|Salesforce>) coming from SF data the user controls.
    if status == "needs_person_details":
        return _escape_mrkdwn(result.get(
            "message",
            "I don't see anyone matching. Can you give me their email or phone?",
        ))
    if status == "ambiguous_person":
        candidates = result.get("candidates", []) or []
        lines = ["I see multiple matches — which one did you mean?"]
        for c in candidates:
            name = _escape_mrkdwn(c.get("name") or c.get("Name") or "?")
            email = _escape_mrkdwn(c.get("email") or c.get("Email") or "(no email)")
            lines.append(f"• *{name}* — {email}")
        return "\n".join(lines)
    if status == "no_sf_identity":
        return _escape_mrkdwn(result.get(
            "message",
            "I can't find a Salesforce user matching your Slack email. "
            "Ask an admin to run `!access map @you <sf_user_id>`.",
        ))
    if status == "invalid_product":
        expected = ", ".join(result.get("expected", []) or [])
        return f"Unknown product. Expected one of: {_escape_mrkdwn(expected)}."
    if status == "invalid_subject":
        return f"Unknown touch subject: `{_escape_mrkdwn(result.get('subject', '?'))}`."
    if status == "no_open_opps":
        return _escape_mrkdwn(result.get("message", "No open opportunities found for that person."))
    if status == "ambiguous_opp":
        candidates = result.get("candidates", []) or []
        lines = ["Multiple open opportunities — which one?"]
        for c in candidates:
            name = _escape_mrkdwn(c.get("name") or c.get("Name") or "?")
            stage = _escape_mrkdwn(c.get("stage") or c.get("StageName") or "?")
            lines.append(f"• *{name}* — {stage}")
        return "\n".join(lines)
    if status == "needs_account":
        return _escape_mrkdwn(result.get(
            "message",
            "That person has no Salesforce Account linked, so I can't create the "
            "opportunity. Set up (or convert) their Account first.",
        ))
    if status == "create_failed":
        err = _escape_mrkdwn(result.get("error", ""))
        return f"Failed to {action}: {err}".strip()
    return f"Couldn't {action}: status `{status}`."


# ── Metrics + history helpers ───────────────────────────────────────────────

def _append_history(key: tuple[str, str, str], user_text: str, assistant_text: str) -> None:
    conversation_store.append_history(
        key, build_history_from_agent_run(user_text, assistant_text)
    )


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
    message_text: str | None = None,
) -> None:
    """Append one JSONL record per agent turn or write confirm/cancel."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "user_id": user_id,
        "channel": channel,
        "thread_ts": thread_ts,
        # Correlation id ties this turn to agent-b's per-tool usage log.
        "correlation_id": result.correlation_id if result else None,
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
    if _LOG_PAYLOADS and message_text is not None:
        record["request"] = message_text
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
