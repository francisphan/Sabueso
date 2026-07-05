"""Sabueso Slack bot — conversational assistant + data exports.

Runs as a separate process using Slack Socket Mode. Users can DM Sabueso
or @mention it in channels to query Salesforce, NetSuite, and Pardot
using plain English.

Sales reps can also ask Sabueso to create Salesforce Opportunities and log
touches. Those write operations halt the agent loop and surface as Block Kit
confirmation cards; the @app.action handlers below dispatch to handlers.py
once the rep clicks Confirm or Cancel.

Usage:
    python src/main.py --bot   # Start the Slack bot
"""

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from handlers import (
    cancel_pending,
    execute_pending,
    handle_direct_message,
    handle_mention,
)
from log_setup import setup_logging

log = logging.getLogger(__name__)


def _create_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # ── Direct Messages ─────────────────────────────────────────────────
    @app.event(
        "message",
        matchers=[
            lambda event: event.get("channel_type") == "im",
            # subtype is None for plain text; "file_share" carries image/file
            # uploads (e.g. a sommelier sending a wine-label photo).
            lambda event: event.get("subtype") in (None, "file_share"),
        ],
    )
    def on_dm(event, say, client, logger):
        logger.info("DM from user=%s files=%d", event["user"], len(event.get("files") or []))
        handle_direct_message(event=event, say=say, client=client)

    # ── @Mentions in channels ───────────────────────────────────────────
    @app.event("app_mention")
    def on_mention(event, say, client, logger):
        logger.info("Mention in channel=%s by user=%s", event["channel"], event["user"])
        handle_mention(event=event, say=say, client=client)

    # ── Confirmation card buttons ───────────────────────────────────────
    @app.action("sabueso_confirm_op")
    def on_confirm(ack, body, client, logger):
        ack()
        action_id = body["actions"][0]["value"]
        logger.info("confirm clicked by user=%s action_id=%s", body["user"]["id"], action_id)
        execute_pending(action_id=action_id, body=body, client=client)

    @app.action("sabueso_cancel_op")
    def on_cancel(ack, body, client, logger):
        ack()
        action_id = body["actions"][0]["value"]
        logger.info("cancel clicked by user=%s action_id=%s", body["user"]["id"], action_id)
        cancel_pending(action_id=action_id, body=body, client=client)

    # ── Catch-all so Bolt doesn't warn about unhandled events ───────────
    @app.event("message")
    def catch_all():
        pass

    return app


def start_bot() -> None:
    """Start the Slack bot in Socket Mode (blocks forever)."""
    setup_logging()
    app = _create_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("Sabueso starting (Socket Mode)…")
    handler.start()
