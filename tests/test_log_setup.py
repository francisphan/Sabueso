"""setup_logging() is the shared entrypoint config: idempotent, quiets httpx,
and keeps the %(guest_tag)s format field resolvable."""

import logging

import pytest

import log_setup
from log_setup import _GuestTagFilter, setup_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    """Snapshot and restore global logging state so these tests don't leak into
    the rest of the suite (setup_logging mutates the root logger)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_noisy = {name: logging.getLogger(name).level for name in log_setup._NOISY_LOGGERS}
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_noisy.items():
            logging.getLogger(name).setLevel(level)


def test_idempotent_no_duplicate_handlers_or_filters():
    setup_logging()
    handler_count = len(logging.root.handlers)

    setup_logging()

    assert len(logging.root.handlers) == handler_count
    for handler in logging.root.handlers:
        tag_filters = [f for f in handler.filters if isinstance(f, _GuestTagFilter)]
        assert len(tag_filters) == 1


def test_httpx_and_httpcore_quieted_to_warning():
    setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_noisy_logger_level_overridable(monkeypatch):
    monkeypatch.setenv("HTTPX_LOG_LEVEL", "INFO")
    setup_logging()
    assert logging.getLogger("httpx").level == logging.INFO


def test_log_level_env_override(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_guest_tag_field_resolves_in_format():
    setup_logging()
    handler = logging.root.handlers[0]
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", None, None)

    for f in handler.filters:
        f.filter(record)

    # The format string references %(guest_tag)s; formatting must not raise and
    # the field is empty when no guest is active (the bot case).
    assert record.guest_tag == ""
    assert "hello" in handler.format(record)


def test_guest_tag_reflects_active_guest():
    from gemini_profiler import current_guest

    token = current_guest.set("Ada Lovelace")
    try:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hi", None, None)
        _GuestTagFilter().filter(record)
        assert record.guest_tag == "[Ada Lovelace] "
    finally:
        current_guest.reset(token)
