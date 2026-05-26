"""Tests for per-turn correlation IDs and their propagation to MCP headers."""

import mcp_client
import tracing


class TestTracing:
    def teardown_method(self):
        tracing.set_correlation_id(None)

    def test_new_turn_id_is_unique_hex(self):
        a, b = tracing.new_turn_id(), tracing.new_turn_id()
        assert a != b
        assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)

    def test_set_and_get(self):
        tracing.set_correlation_id("turn-1")
        assert tracing.get_correlation_id() == "turn-1"

    def test_default_is_none(self):
        tracing.set_correlation_id(None)
        assert tracing.get_correlation_id() is None


class TestHeaderPropagation:
    def teardown_method(self):
        tracing.set_correlation_id(None)

    def test_header_present_when_correlation_set(self):
        tracing.set_correlation_id("turn-xyz")
        assert mcp_client._headers()["X-Correlation-ID"] == "turn-xyz"

    def test_header_absent_when_no_correlation(self):
        tracing.set_correlation_id(None)
        assert "X-Correlation-ID" not in mcp_client._headers()
