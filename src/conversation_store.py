"""Per-conversation history store — Redis-backed so it survives restarts.

The bot keeps a short rolling history (last N turns) per conversation to give
Claude multi-turn context. Holding that only in process memory means every
redeploy/restart wipes it mid-chat, which reads as the bot losing its memory.

When ``REDIS_URL`` is set, history is persisted in a Redis list per conversation
(RPUSH + LTRIM to the last N turns, with a TTL); otherwise it falls back to an
in-process dict for local dev. All operations are best-effort: a Redis hiccup
degrades to "no history" rather than breaking the turn.
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

# Keep the last _MAX_TURNS turns (one turn == a user message + an assistant
# message, so two list entries).
_MAX_TURNS = 20
_MAX_ENTRIES = _MAX_TURNS * 2
# Idle conversations expire after this many seconds (refreshed on every write).
_TTL_SECONDS = 14 * 24 * 3600
_KEY_PREFIX = "sabueso:convo:"

_redis = None
_redis_initialized = False
_mem: dict[str, list[dict]] = {}
_mem_lock = threading.Lock()


def _client():
    """Lazily build the Redis client; return None to use the in-memory fallback."""
    global _redis, _redis_initialized
    if _redis_initialized:
        return _redis
    _redis_initialized = True
    url = os.getenv("REDIS_URL")
    if not url:
        log.info("Conversation store: in-memory (REDIS_URL unset)")
        return None
    try:
        import redis

        client = redis.from_url(url, decode_responses=True)
        client.ping()
        _redis = client
        log.info("Conversation store: Redis")
    except Exception:
        log.exception("Conversation store: Redis init failed — using in-memory")
        _redis = None
    return _redis


def _skey(key: tuple[str, str, str]) -> str:
    user, channel, thread = key
    return f"{_KEY_PREFIX}{user}:{channel}:{thread}"


def get_history(key: tuple[str, str, str]) -> list[dict]:
    """Return the stored history entries for a conversation (oldest first)."""
    r = _client()
    sk = _skey(key)
    if r is None:
        with _mem_lock:
            return list(_mem.get(sk, []))
    try:
        out: list[dict] = []
        for item in r.lrange(sk, 0, -1):
            try:
                out.append(json.loads(item))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        log.exception("Conversation store: read failed — returning empty history")
        return []


def append_history(key: tuple[str, str, str], entries: list[dict]) -> None:
    """Append history entries, trimming to the last N turns. Best-effort."""
    if not entries:
        return
    r = _client()
    sk = _skey(key)
    if r is None:
        with _mem_lock:
            hist = _mem.setdefault(sk, [])
            hist.extend(entries)
            if len(hist) > _MAX_ENTRIES:
                _mem[sk] = hist[-_MAX_ENTRIES:]
        return
    try:
        pipe = r.pipeline()
        pipe.rpush(sk, *[json.dumps(e, default=str) for e in entries])
        pipe.ltrim(sk, -_MAX_ENTRIES, -1)
        pipe.expire(sk, _TTL_SECONDS)
        pipe.execute()
    except Exception:
        log.exception("Conversation store: append failed — history not persisted")
