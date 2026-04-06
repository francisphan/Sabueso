"""Sabueso Slack bot — conversational assistant + data exports.

Runs as a separate process using Slack Socket Mode. Users can DM Sabueso
or @mention it in channels to query Salesforce, NetSuite, and Pardot
using plain English.

Usage:
    python src/main.py --bot   # Start the Slack bot
"""

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from handlers import handle_direct_message, handle_mention

log = logging.getLogger(__name__)


def _create_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    # ── Direct Messages ─────────────────────────────────────────────────
    @app.event(
        "message",
        matchers=[
            lambda event: event.get("channel_type") == "im",
            lambda event: event.get("subtype") is None,
        ],
    )
    def on_dm(event, say, client, logger):
        logger.info("DM from user=%s", event["user"])
        handle_direct_message(event=event, say=say, client=client)

    # ── @Mentions in channels ───────────────────────────────────────────
    @app.event("app_mention")
    def on_mention(event, say, client, logger):
        logger.info("Mention in channel=%s by user=%s", event["channel"], event["user"])
        handle_mention(event=event, say=say, client=client)

    # ── Catch-all so Bolt doesn't warn about unhandled events ───────────
    @app.event("message")
    def catch_all():
        pass

    return app


def start_bot() -> None:
    """Start the Slack bot in Socket Mode (blocks forever)."""
    app = _create_app()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    log.info("Sabueso starting (Socket Mode)…")
    handler.start()
