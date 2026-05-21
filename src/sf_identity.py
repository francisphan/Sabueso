"""
Resolve a Slack user to their Salesforce User ID.

Order of resolution:
  1. Manual override stored in the ACL (`!access map @user <sf_user_id>`).
  2. Cached lookup in `.cache/sf_user_map.json`.
  3. Slack profile email → SF User.Email match (SOQL).

The cache lets us avoid hitting Slack's users.info and SF on every write.
Misses return None — callers should surface a friendly error so an admin
can resolve via `!access map`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import permissions
from mcp_client import call_tool

log = logging.getLogger(__name__)

_CACHE_PATH = Path(os.getenv("SF_USER_MAP_FILE", ".cache/sf_user_map.json"))
_cache_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    if _CACHE_PATH.exists():
        try:
            _cache = json.loads(_CACHE_PATH.read_text())
            return _cache
        except json.JSONDecodeError:
            log.warning("Corrupt SF user map at %s — starting fresh", _CACHE_PATH)
    _cache = {}
    return _cache


def _persist_cache(cache: dict[str, str]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except OSError:
        log.exception("Failed to persist SF user map to %s", _CACHE_PATH)


def _get_slack_email(slack_user_id: str, slack_client: Any) -> str | None:
    try:
        info = slack_client.users_info(user=slack_user_id)
    except Exception:
        log.exception("slack users_info failed for %s", slack_user_id)
        return None
    profile = info.get("user", {}).get("profile", {})
    email = profile.get("email")
    return email.strip() if email else None


def _soql_lookup_by_email(email: str) -> str | None:
    """Return the SF User.Id for an email if exactly one active match exists."""
    escaped = email.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"SELECT Id FROM User WHERE Email = '{escaped}' "
        f"AND IsActive = true LIMIT 2"
    )
    try:
        result = call_tool("sf_soql_query", {"query_str": query})
    except Exception:
        log.exception("SF user lookup failed for email %s", email)
        return None

    records = _records(result)
    if len(records) != 1:
        if len(records) > 1:
            log.warning("Multiple active SF users found for email %s — refusing to guess", email)
        return None
    return records[0].get("Id")


def _records(result: Any) -> list[dict]:
    """Normalize the SF query result envelope."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if "records" in result and isinstance(result["records"], list):
            return result["records"]
        # Some envelopes wrap under "result"
        inner = result.get("result")
        if isinstance(inner, list):
            return inner
    return []


def get_sf_user_id_for_slack(slack_user_id: str, slack_client: Any) -> str | None:
    """Resolve a Slack user ID to an SF User ID, or None on miss."""
    override = permissions.get_sf_user_override(slack_user_id)
    if override:
        return override

    with _cache_lock:
        cache = _load_cache()
        cached = cache.get(slack_user_id)
        if cached:
            return cached

    email = _get_slack_email(slack_user_id, slack_client)
    if not email:
        log.info("No Slack email available for %s", slack_user_id)
        return None

    sf_user_id = _soql_lookup_by_email(email)
    if sf_user_id is None:
        return None

    with _cache_lock:
        cache = _load_cache()
        cache[slack_user_id] = sf_user_id
        _persist_cache(cache)

    return sf_user_id


def clear_cache_entry(slack_user_id: str) -> None:
    """Forget a cached mapping. Useful when an admin reassigns SF identity."""
    with _cache_lock:
        cache = _load_cache()
        if slack_user_id in cache:
            del cache[slack_user_id]
            _persist_cache(cache)
