"""Post Slack review messages for guests with new profile information."""

import json
import logging
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

_VALUE_MAX_CHARS = 2000


def _build_button_value(guest: dict) -> str:
    """Build a JSON payload for button values, truncating bio if needed."""
    profile = guest.get("profile", {})
    value = {}

    account_id = guest.get("account_id")
    if account_id:
        value["account_id"] = account_id

    value["guest_name"] = f"{guest.get('first_name', '')} {guest.get('last_name', '')}".strip()

    for key in ("new_title", "new_website", "new_photo_url"):
        v = guest.get(key)
        if v:
            # button value uses photo_url not new_photo_url
            out_key = "photo_url" if key == "new_photo_url" else key
            value[out_key] = v

    links = profile.get("links")
    if links:
        value["links"] = links

    # Add new_bio last so we can truncate it to fit the limit
    new_bio = guest.get("new_bio")
    if new_bio:
        value["new_bio"] = new_bio

    serialized = json.dumps(value)
    if len(serialized) <= _VALUE_MAX_CHARS:
        return serialized

    # Truncate new_bio to fit
    if "new_bio" in value:
        overhead = len(json.dumps({k: v for k, v in value.items() if k != "new_bio"}))
        # Account for key, quotes, comma: ',"new_bio":"..."'
        wrapper_len = len(',"new_bio":""')
        available = _VALUE_MAX_CHARS - overhead - wrapper_len
        if available > 20:
            value["new_bio"] = new_bio[: available - 3] + "..."
        else:
            del value["new_bio"]

    return json.dumps(value)[:_VALUE_MAX_CHARS]


def _build_blocks(guest: dict) -> list[dict]:
    """Build Block Kit blocks for a single guest review message."""
    profile = guest.get("profile", {})
    first = guest.get("first_name", "")
    last = guest.get("last_name", "")
    full_name = f"{first} {last}".strip()

    # 1. Header
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": full_name}},
    ]

    # 2. Guest details section
    details_parts = []
    if guest.get("check_in"):
        details_parts.append(f"*Check-in:* {guest['check_in']}")
    if guest.get("villa"):
        details_parts.append(f"*Villa:* {guest['villa']}")
    if guest.get("stay_count") is not None:
        details_parts.append(f"*Stay count:* {guest['stay_count']}")
    location_parts = [p for p in [guest.get("city"), guest.get("state"), guest.get("country")] if p]
    if location_parts:
        details_parts.append(f"*Location:* {', '.join(location_parts)}")

    details_section: dict = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(details_parts) or "_No details_"},
    }
    photo_url = profile.get("photo_url") or guest.get("new_photo_url")
    if photo_url:
        details_section["accessory"] = {
            "type": "image",
            "image_url": photo_url,
            "alt_text": full_name,
        }
    blocks.append(details_section)

    # 3. New findings section (new_* fields live on guest dict, not inside profile)
    findings = []
    if guest.get("new_title"):
        findings.append(f"Found title: {guest['new_title']}")
    if guest.get("new_photo_url"):
        findings.append("Found photo")
    if guest.get("new_bio"):
        bio_preview = guest["new_bio"][:200]
        if len(guest["new_bio"]) > 200:
            bio_preview += "…"
        findings.append(f"New bio info: {bio_preview}")
    if guest.get("new_website"):
        findings.append(f"Found website: {guest['new_website']}")

    if findings:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*New Findings*\n" + "\n".join(f"• {f}" for f in findings)},
        })

    # 4. Context block with links
    links = profile.get("links", [])
    if links:
        links_text = " | ".join(f"<{url}>" for url in links[:10])
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Links explored: {links_text}"}],
        })

    # 5. Actions block
    button_value = _build_button_value(guest)
    actions: list[dict] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve All"},
            "style": "primary",
            "action_id": "sabueso_approve_all",
            "value": button_value,
        },
    ]
    if guest.get("new_bio"):
        actions.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve Bio Only"},
            "action_id": "sabueso_approve_bio",
            "value": button_value,
        })
    if guest.get("new_photo_url"):
        actions.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve Photo Only"},
            "action_id": "sabueso_approve_photo",
            "value": button_value,
        })
    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "Reject"},
        "style": "danger",
        "action_id": "sabueso_reject",
        "value": button_value,
    })

    blocks.append({"type": "actions", "elements": actions})

    return blocks


def post_review_messages(profiled_guests: list[dict]) -> None:
    """Post a Block Kit review message for each guest with new info.

    Requires SLACK_BOT_TOKEN env var. SLACK_REVIEW_CHANNEL defaults to
    ``#sabueso-review`` if not set.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set — skipping Slack notifications.")
        return

    channel = os.environ.get("SLACK_REVIEW_CHANNEL", "#sabueso-review")
    client = WebClient(token=token)

    guests_with_new_info = [g for g in profiled_guests if g.get("has_new_info")]
    if not guests_with_new_info:
        log.info("No guests with new info — no Slack messages to send.")
        return

    log.info("Posting %d Slack review message(s) to %s…", len(guests_with_new_info), channel)
    for guest in guests_with_new_info:
        name = f"{guest.get('first_name', '')} {guest.get('last_name', '')}".strip()
        try:
            blocks = _build_blocks(guest)
            client.chat_postMessage(
                channel=channel,
                text=f"Review: {name}",  # fallback for notifications
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False,
            )
            log.info("Posted review message for %s.", name)
        except SlackApiError:
            log.error("Failed to post Slack message for %s.", name, exc_info=True)
        except Exception:
            log.error("Unexpected error posting Slack message for %s.", name, exc_info=True)
