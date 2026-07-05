"""Shared logging configuration for every Sabueso entrypoint.

Both the report pipeline (scheduler) and the Slack bot call setup_logging()
explicitly at startup so neither depends on an import side effect for its log
format. Previously only scheduler.py ran logging.basicConfig at module load, and
the bot inherited it by accident because main.py imports scheduler at the top.
"""

import logging
import os

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(guest_tag)s%(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that emit one INFO line per HTTP call — noisy at our INFO
# baseline (httpx logs every Claude/Gemini request). Quiet them to WARNING; each
# is overridable via <NAME>_LOG_LEVEL (e.g. HTTPX_LOG_LEVEL=INFO).
_NOISY_LOGGERS = ("httpx", "httpcore")


def _resolve_level(value: str | None, default: int) -> int:
    """Translate a level name ("INFO") or number ("10") from env into an int."""
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return int(value)
    resolved = logging.getLevelName(value.upper())
    return resolved if isinstance(resolved, int) else default


def _guest_tag() -> str:
    """The active guest name wrapped as a log prefix, or "" when none is set.

    Read lazily from gemini_profiler's contextvar so this module stays free of
    heavy imports and remains testable in isolation. The bot process never sets
    a guest, so the tag is always empty there.
    """
    try:
        from gemini_profiler import current_guest

        guest = current_guest.get("")
    except Exception:
        return ""
    return f"[{guest}] " if guest else ""


class _GuestTagFilter(logging.Filter):
    """Ensure every record carries a `guest_tag` attribute so the format's
    %(guest_tag)s placeholder always resolves (empty when no guest is active)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.guest_tag = _guest_tag()
        return True


def setup_logging() -> None:
    """Configure root logging for a Sabueso entrypoint. Idempotent.

    Level comes from LOG_LEVEL (default INFO). Every root handler gets the
    guest-tag filter so `%(guest_tag)s` always resolves, and httpx/httpcore are
    quieted to WARNING. Safe to call more than once — no duplicate handlers or
    filters accumulate.
    """
    level = _resolve_level(os.environ.get("LOG_LEVEL"), logging.INFO)

    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    # basicConfig is a no-op once handlers exist, so set the level explicitly to
    # stay correct across repeated calls.
    logging.getLogger().setLevel(level)

    for handler in logging.root.handlers:
        if not any(isinstance(f, _GuestTagFilter) for f in handler.filters):
            handler.addFilter(_GuestTagFilter())

    for name in _NOISY_LOGGERS:
        override = os.environ.get(f"{name.upper()}_LOG_LEVEL")
        logging.getLogger(name).setLevel(_resolve_level(override, logging.WARNING))
