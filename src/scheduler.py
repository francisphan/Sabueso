"""Monday/Thursday scheduler that runs the guest report pipeline."""

import os
import time
import logging

import schedule

from salesforce_client import fetch_upcoming_guests
from gemini_profiler import profile_guests
from report_builder import build_html
from email_sender import send_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def run_report() -> None:
    """Execute the full pipeline: fetch → profile → build → send."""
    log.info("Starting guest report pipeline…")

    log.info("Fetching upcoming guests from Salesforce…")
    guests = fetch_upcoming_guests()
    log.info("Found %d upcoming guest(s).", len(guests))

    log.info("Profiling guests with Gemini…")
    profiled = profile_guests(guests)

    log.info("Building HTML report…")
    html = build_html(profiled)

    subscribers_raw = os.environ.get("REPORT_SUBSCRIBERS", "")
    recipients = [s.strip() for s in subscribers_raw.split(",") if s.strip()]
    if not recipients:
        log.warning("REPORT_SUBSCRIBERS is empty — no email sent.")
        return

    subject = f"Sabueso Guest Report — {len(profiled)} on property & arriving this week"
    log.info("Sending report to %d subscriber(s)…", len(recipients))
    send_report(subject=subject, html_body=html, recipients=recipients)
    log.info("Done.")


def start_scheduler() -> None:
    """Block forever, running the report every Monday and Thursday at 08:00."""
    schedule.every().monday.at("08:00").do(run_report)
    schedule.every().thursday.at("08:00").do(run_report)

    log.info("Scheduler started. Report will run every Monday and Thursday at 08:00.")
    while True:
        schedule.run_pending()
        time.sleep(60)
