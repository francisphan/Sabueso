"""Tests for audit — append-only JSONL audit trail."""

from __future__ import annotations

import json
import threading

import pytest

import audit
from audit import record_write


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch, tmp_path):
    """Redirect _AUDIT_PATH to a temp location for each test."""
    audit_file = tmp_path / "cache" / "write_audit.jsonl"
    monkeypatch.setattr(audit, "_AUDIT_PATH", audit_file)
    return audit_file


# ── record_write basics ────────────────────────────────────────────────────

class TestRecordWrite:
    def test_appends_one_line_per_call(self, isolated_audit):
        record_write({"user": "U1", "tool": "sf_create_record", "result": "ok"})
        lines = isolated_audit.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_multiple_writes_append_separate_lines(self, isolated_audit):
        for i in range(3):
            record_write({"seq": i})
        lines = isolated_audit.read_text().strip().splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            rec = json.loads(line)
            assert rec["seq"] == i

    def test_each_line_is_valid_json(self, isolated_audit):
        record_write({"user": "U1", "tool": "sf_log_touch"})
        line = isolated_audit.read_text().strip()
        parsed = json.loads(line)
        assert parsed["user"] == "U1"
        assert parsed["tool"] == "sf_log_touch"


# ── timestamp handling ─────────────────────────────────────────────────────

class TestTimestamp:
    def test_auto_adds_ts_if_missing(self, isolated_audit):
        record_write({"user": "U1"})
        rec = json.loads(isolated_audit.read_text().strip())
        assert "ts" in rec
        assert rec["ts"]  # non-empty

    def test_preserves_provided_ts(self, isolated_audit):
        record_write({"user": "U1", "ts": "2026-01-01T00:00:00+00:00"})
        rec = json.loads(isolated_audit.read_text().strip())
        assert rec["ts"] == "2026-01-01T00:00:00+00:00"

    def test_auto_ts_is_iso_format(self, isolated_audit):
        record_write({"user": "U1"})
        rec = json.loads(isolated_audit.read_text().strip())
        # Should parse as ISO datetime without raising
        from datetime import datetime
        datetime.fromisoformat(rec["ts"])


# ── Concurrency ────────────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_writes_all_land(self, isolated_audit):
        """10 threads writing simultaneously — all 10 lines must be present."""
        N = 10
        barrier = threading.Barrier(N)

        def write_one(i: int) -> None:
            barrier.wait()
            record_write({"thread": i})

        threads = [threading.Thread(target=write_one, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = isolated_audit.read_text().strip().splitlines()
        assert len(lines) == N

        # All lines must be valid JSON
        parsed = [json.loads(line) for line in lines]
        thread_ids = {rec["thread"] for rec in parsed}
        assert thread_ids == set(range(N))


# ── OSError resilience ─────────────────────────────────────────────────────

class TestOsErrorResilience:
    def test_oserror_does_not_raise(self, monkeypatch, tmp_path):
        """If the audit file path is unwritable, record_write must not raise."""
        # Make _AUDIT_PATH a directory — open() on a dir raises IsADirectoryError
        bad_path = tmp_path / "is_a_dir"
        bad_path.mkdir()
        monkeypatch.setattr(audit, "_AUDIT_PATH", bad_path)

        # Should log the error but not propagate the exception
        record_write({"user": "U1", "should": "not_raise"})  # must not raise
