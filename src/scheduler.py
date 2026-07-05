"""Monday/Thursday scheduler that runs the guest report pipeline."""

import json
import os
import time
import logging
from pathlib import Path

import schedule

from salesforce_client import fetch_upcoming_guests
from gemini_profiler import profile_guests
from report_builder import build_html
from email_sender import send_report
from log_setup import setup_logging

log = logging.getLogger(__name__)


def run_report(
    recipients_override: list[str] | None = None,
    cache_dir: Path | None = None,
    run_id: int | None = None,
) -> None:
    """Execute the full pipeline: fetch → profile → build → send.

    Args:
        recipients_override: If set, send only to these addresses instead of REPORT_SUBSCRIBERS.
        cache_dir: If set, cache the Salesforce guest list here (reused across runs) and
                   save each run's profile results as run_{run_id}_profiles.json.
        run_id: Label for this run when saving cached profiles (e.g. 1, 2, 3).
    """
    setup_logging()
    log.info("Starting guest report pipeline…")

    # Fetch guests — reuse cached list if available (avoids redundant Salesforce calls)
    sf_cache = cache_dir / "guests.json" if cache_dir else None
    if sf_cache and sf_cache.exists():
        guests = json.loads(sf_cache.read_text())
        log.info("Loaded %d guest(s) from Salesforce cache (%s).", len(guests), sf_cache)
    else:
        log.info("Fetching upcoming guests from Salesforce…")
        guests = fetch_upcoming_guests()
        if sf_cache:
            sf_cache.write_text(json.dumps(guests, default=str, indent=2))
            log.info("Cached %d guest(s) to %s.", len(guests), sf_cache)

    log.info("Found %d upcoming guest(s).", len(guests))
    for g in guests:
        log.info("  %s %s | check-in: %s | check-out: %s | villa: %s",
            g["first_name"], g["last_name"], g["check_in"], g["check_out"], g["villa"] or "—")

    log.info("Profiling guests…")
    profiled = profile_guests(guests)

    # Save per-run profiles for later comparison
    if cache_dir and run_id is not None:
        profile_cache = cache_dir / f"run_{run_id}_profiles.json"
        profile_cache.write_text(json.dumps(profiled, default=str, indent=2))
        log.info("Saved run %d profiles to %s.", run_id, profile_cache)

    log.info("Building HTML report…")
    html = build_html(profiled)
    log.info("Report built (%d bytes).", len(html))

    recipients = recipients_override
    if not recipients:
        subscribers_raw = os.environ.get("REPORT_SUBSCRIBERS", "")
        recipients = [s.strip() for s in subscribers_raw.split(",") if s.strip()]
    if not recipients:
        log.warning("No recipients configured — no email sent.")
        return

    run_label = f" [run {run_id}]" if run_id is not None else ""
    subject = f"Sabueso Guest Report{run_label} - {len(profiled)} guests on property & arriving this week"
    log.info("Sending report to %d recipient(s): %s", len(recipients), ", ".join(recipients))
    send_report(subject=subject, html_body=html, recipients=recipients)
    log.info("Report sent successfully. Pipeline complete.")


def start_scheduler() -> None:
    """Block forever, running the report every Monday and Thursday at 08:00."""
    setup_logging()
    schedule.every().monday.at("08:00").do(run_report)
    schedule.every().thursday.at("08:00").do(run_report)

    log.info("Scheduler started. Report will run every Monday and Thursday at 08:00.")
    while True:
        schedule.run_pending()
        time.sleep(60)
