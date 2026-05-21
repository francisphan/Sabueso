"""Tests for pending_ops — TTL-bounded pending write-operation store."""

from __future__ import annotations

import threading
import time as real_time

import pytest

import pending_ops
from pending_ops import PendingOp, peek, pop, register

# ── Helpers ────────────────────────────────────────────────────────────────

_REGISTER_DEFAULTS = dict(
    tool_name="sf_create_opportunity_for_person",
    arguments={"name": "Alice"},
    summary="Create opportunity for Alice",
    requester_user_id="USLACK1",
    channel="C123",
    thread_ts="1234567890.123456",
)


@pytest.fixture(autouse=True)
def clear_pending():
    """Wipe the shared _pending dict before and after each test."""
    pending_ops._pending.clear()
    yield
    pending_ops._pending.clear()


# ── register ───────────────────────────────────────────────────────────────

class TestRegister:
    def test_returns_pending_op_with_action_id(self):
        op = register(**_REGISTER_DEFAULTS)
        assert isinstance(op, PendingOp)
        assert op.action_id
        assert len(op.action_id) == 32  # uuid4().hex length

    def test_each_register_returns_unique_action_id(self):
        op1 = register(**_REGISTER_DEFAULTS)
        op2 = register(**_REGISTER_DEFAULTS)
        assert op1.action_id != op2.action_id

    def test_expires_at_is_future(self):
        before = real_time.time()
        op = register(**_REGISTER_DEFAULTS)
        after = real_time.time()
        assert op.expires_at > before
        assert op.expires_at <= after + pending_ops._TTL_SECONDS + 1

    def test_custom_ttl(self):
        op = register(**_REGISTER_DEFAULTS, ttl_seconds=60)
        assert abs(op.expires_at - (real_time.time() + 60)) < 2

    def test_fields_stored_correctly(self):
        op = register(**_REGISTER_DEFAULTS)
        assert op.tool_name == "sf_create_opportunity_for_person"
        assert op.arguments == {"name": "Alice"}
        assert op.summary == "Create opportunity for Alice"
        assert op.requester_user_id == "USLACK1"
        assert op.channel == "C123"
        assert op.thread_ts == "1234567890.123456"

    def test_thread_ts_can_be_none(self):
        op = register(**{**_REGISTER_DEFAULTS, "thread_ts": None})
        assert op.thread_ts is None


# ── pop ────────────────────────────────────────────────────────────────────

class TestPop:
    def test_pop_returns_op_once(self):
        op = register(**_REGISTER_DEFAULTS)
        result = pop(op.action_id)
        assert result is not None
        assert result.action_id == op.action_id

    def test_pop_removes_op_second_call_returns_none(self):
        op = register(**_REGISTER_DEFAULTS)
        pop(op.action_id)
        assert pop(op.action_id) is None

    def test_pop_unknown_action_id_returns_none(self):
        assert pop("nonexistent_action_id_abc123") is None

    def test_pop_expired_entry_returns_none(self, monkeypatch):
        op = register(**_REGISTER_DEFAULTS)
        # Advance time past the op's expiry
        monkeypatch.setattr(pending_ops.time, "time", lambda: op.expires_at + 1)
        result = pop(op.action_id)
        assert result is None

    def test_pop_expired_entry_is_purged(self, monkeypatch):
        op = register(**_REGISTER_DEFAULTS)
        monkeypatch.setattr(pending_ops.time, "time", lambda: op.expires_at + 1)
        pop(op.action_id)
        assert op.action_id not in pending_ops._pending


# ── peek ───────────────────────────────────────────────────────────────────

class TestPeek:
    def test_peek_is_non_destructive(self):
        op = register(**_REGISTER_DEFAULTS)
        peeked = peek(op.action_id)
        assert peeked is not None
        assert peeked.action_id == op.action_id

        # Op is still poppable
        popped = pop(op.action_id)
        assert popped is not None
        assert popped.action_id == op.action_id

    def test_peek_unknown_returns_none(self):
        assert peek("does_not_exist") is None

    def test_peek_does_not_consume(self):
        op = register(**_REGISTER_DEFAULTS)
        for _ in range(5):
            assert peek(op.action_id) is not None
        assert pop(op.action_id) is not None


# ── Concurrency ────────────────────────────────────────────────────────────

class TestConcurrency:
    def test_100_concurrent_registers_yield_unique_ids(self):
        N_THREADS = 10
        OPS_PER_THREAD = 10
        barrier = threading.Barrier(N_THREADS)
        results: list[str] = []
        lock = threading.Lock()

        def register_batch() -> None:
            barrier.wait()
            ids = [register(**_REGISTER_DEFAULTS).action_id for _ in range(OPS_PER_THREAD)]
            with lock:
                results.extend(ids)

        threads = [threading.Thread(target=register_batch) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == N_THREADS * OPS_PER_THREAD
        assert len(set(results)) == N_THREADS * OPS_PER_THREAD
