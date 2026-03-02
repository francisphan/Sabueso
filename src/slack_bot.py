"""Slack bot that listens for Sabueso approval button clicks and writes to Salesforce.

Runs as a separate process using Slack Socket Mode. Handles the approval/rejection
buttons posted by slack_notifier.py in #sabueso-review.

Usage:
    python src/main.py --bot   # Start the Slack approval bot
"""

import json
import logging
import os
from datetime import datetime

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from salesforce_client import update_account, upload_photo_to_account

log = logging.getLogger(__name__)


def _create_app() -> App:
    """Create and configure the Slack Bolt app with all Sabueso action handlers."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    @app.action("sabueso_approve_all")
    def handle_approve_all(ack, action, body, client):
        ack()
        payload = json.loads(action["value"])
        account_id = payload.get("account_id")
        guest_name = payload.get("guest_name", "Unknown")
        user_id = body["user"]["id"]
        user_name = body["user"].get("username", body["user"].get("name", "unknown"))

        if not account_id:
            _update_message(client, body, f":warning: *{guest_name}* — missing Account ID. No changes made.")
            return

        errors = []
        written = []

        if payload.get("new_bio"):
            try:
                _append_description(account_id, payload["new_bio"], user_name)
                written.append("bio")
            except Exception as e:
                log.error("Failed to append bio for %s: %s", guest_name, e, exc_info=True)
                errors.append("bio")

        fields = {}
        if payload.get("new_title"):
            fields["PersonTitle"] = payload["new_title"]
        if payload.get("new_website"):
            fields["Website"] = payload["new_website"]
        if fields:
            try:
                update_account(account_id, fields)
                written.extend(fields.keys())
            except Exception as e:
                log.error("Failed to update fields for %s: %s", guest_name, e, exc_info=True)
                errors.extend(fields.keys())

        if payload.get("photo_url"):
            try:
                _upload_photo(account_id, payload["photo_url"])
                written.append("photo")
            except Exception as e:
                log.error("Failed to upload photo for %s: %s", guest_name, e, exc_info=True)
                errors.append("photo")

        if errors:
            msg = f":warning: *{guest_name}* — partially approved by <@{user_id}>. Wrote: {', '.join(written) or 'nothing'}. Failed: {', '.join(errors)}."
        else:
            msg = f":white_check_mark: *{guest_name}* — approved by <@{user_id}>. Updated: {', '.join(written)}."

        _update_message(client, body, msg)

    @app.action("sabueso_approve_bio")
    def handle_approve_bio(ack, action, body, client):
        ack()
        payload = json.loads(action["value"])
        account_id = payload.get("account_id")
        guest_name = payload.get("guest_name", "Unknown")
        user_id = body["user"]["id"]
        user_name = body["user"].get("username", body["user"].get("name", "unknown"))

        if not account_id or not payload.get("new_bio"):
            _update_message(client, body, f":warning: *{guest_name}* — no bio data to write.")
            return

        try:
            _append_description(account_id, payload["new_bio"], user_name)
            _update_message(client, body, f":white_check_mark: *{guest_name}* — bio approved by <@{user_id}>. Description updated.")
        except Exception as e:
            log.error("Failed to append bio for %s: %s", guest_name, e, exc_info=True)
            _update_message(client, body, f":x: *{guest_name}* — failed to update bio: {e}")

    @app.action("sabueso_approve_photo")
    def handle_approve_photo(ack, action, body, client):
        ack()
        payload = json.loads(action["value"])
        account_id = payload.get("account_id")
        guest_name = payload.get("guest_name", "Unknown")
        user_id = body["user"]["id"]

        if not account_id or not payload.get("photo_url"):
            _update_message(client, body, f":warning: *{guest_name}* — no photo data to upload.")
            return

        try:
            _upload_photo(account_id, payload["photo_url"])
            _update_message(client, body, f":white_check_mark: *{guest_name}* — photo approved by <@{user_id}>. Uploaded to Salesforce Files.")
        except Exception as e:
            log.error("Failed to upload photo for %s: %s", guest_name, e, exc_info=True)
            _update_message(client, body, f":x: *{guest_name}* — failed to upload photo: {e}")

    @app.action("sabueso_reject")
    def handle_reject(ack, action, body, client):
        ack()
        payload = json.loads(action["value"])
        guest_name = payload.get("guest_name", "Unknown")
        user_id = body["user"]["id"]
        _update_message(client, body, f":x: *{guest_name}* — rejected by <@{user_id}>. Wrong person — no changes made.")

    return app


def _append_description(account_id: str, new_text: str, approver_name: str) -> None:
    """Read existing Description, append new text with Sabueso separator, and update."""
    from simple_salesforce import Salesforce
    from salesforce_client import _connect

    sf = _connect()
    result = sf.query(f"SELECT Description FROM Account WHERE Id = '{account_id}'")
    records = result.get("records", [])
    current = records[0].get("Description", "") or "" if records else ""

    date_str = datetime.now().strftime("%b %-d, %Y")
    separator = f"\n\n---\n[Sabueso - {date_str} via @{approver_name}]"
    updated = current + separator + "\n" + new_text

    update_account(account_id, {"Description": updated})


def _upload_photo(account_id: str, photo_url: str) -> None:
    """Download photo from URL and upload to Salesforce Files linked to Account."""
    resp = requests.get(photo_url, timeout=30, headers={"User-Agent": "SabuesoBot/1.0"})
    resp.raise_for_status()

    # Determine filename from URL
    from urllib.parse import urlparse
    path = urlparse(photo_url).path
    filename = path.split("/")[-1] or "photo.jpg"
    if not any(filename.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
        filename += ".jpg"

    # Determine MIME type
    lower = filename.lower()
    if lower.endswith(".png"):
        mime = "image/png"
    elif lower.endswith(".webp"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    upload_photo_to_account(account_id, resp.content, filename, mime)


def _update_message(client, body: dict, text: str) -> None:
    """Replace the original Slack message with a confirmation/error message."""
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text},
                }
            ],
        )
    except Exception:
        log.error("Failed to update Slack message", exc_info=True)


def start_bot() -> None:
    """Start the Slack bot in Socket Mode (blocks forever)."""
    app = _create_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("Sabueso approval bot starting (Socket Mode)…")
    handler.start()
