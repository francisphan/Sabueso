"""
Append-only audit trail for bot-initiated Salesforce writes.

One JSONL record per executed write, keyed by Slack user, tool, and result.
Lives at `.cache/write_audit.jsonl` by default; override via WRITE_AUDIT_FILE.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_AUDIT_PATH = Path(os.getenv("WRITE_AUDIT_FILE", ".cache/write_audit.jsonl"))
_lock = threading.Lock()


def record_write(payload: dict[str, Any]) -> None:
    """Append a single audit record. Adds a timestamp if not provided."""
    record = dict(payload)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    line = json.dumps(record, default=str)
    try:
        with _lock:
            _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        log.exception("Failed to write audit record to %s", _AUDIT_PATH)
