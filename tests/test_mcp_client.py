"""Tests for the MCP HTTP client's SSE parsing and dead-session retry."""

import json
from unittest.mock import MagicMock, patch

import pytest

import mcp_client
from mcp_client import MCPNoResponseError, _parse_sse_response


def _sse_response(messages, status=200):
    """Build a mock requests.Response streaming the given JSON-RPC messages."""
    lines = []
    for m in messages:
        lines.append("data: " + json.dumps(m))
        lines.append("")
    resp = MagicMock()
    resp.status_code = status
    resp.iter_lines.return_value = iter(lines)
    resp.raise_for_status.return_value = None
    return resp


class TestParseSseResponse:
    def test_matching_result_returned(self):
        resp = _sse_response(
            [{"jsonrpc": "2.0", "id": "req-1", "result": {"content": [{"type": "text", "text": '{"ok": true}'}]}}]
        )
        assert _parse_sse_response(resp, "req-1") == {"ok": True}

    def test_matching_error_raises_valueerror(self):
        resp = _sse_response([{"jsonrpc": "2.0", "id": "req-1", "error": {"code": -32000, "message": "boom"}}])
        with pytest.raises(ValueError, match="MCP error -32000"):
            _parse_sse_response(resp, "req-1")

    def test_empty_stream_raises_no_response(self):
        resp = _sse_response([])
        with pytest.raises(MCPNoResponseError, match="No response received"):
            _parse_sse_response(resp, "req-1")

    def test_stray_error_surfaced(self):
        # A dead session comes back as a JSON-RPC error with id null — the
        # message must carry that detail instead of the blind "no response".
        resp = _sse_response(
            [{"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Session terminated"}}]
        )
        with pytest.raises(MCPNoResponseError, match="Session terminated"):
            _parse_sse_response(resp, "req-1")


class TestDeadSessionRetry:
    def setup_method(self):
        mcp_client._session_id = "stale-session"

    def teardown_method(self):
        mcp_client._session_id = None

    @patch("mcp_client._initialize")
    @patch("mcp_client.requests.post")
    def test_read_tool_retries_once_on_no_response(self, mock_post, mock_init):
        dead = _sse_response([])  # first attempt: stream with no matching message
        ok = _sse_response(
            [{"jsonrpc": "2.0", "id": "x", "result": {"content": [{"type": "text", "text": '{"n": 1}'}]}}]
        )
        mock_post.side_effect = [dead, ok]

        with patch("mcp_client.uuid.uuid4", return_value="x"):
            result = mcp_client.call_tool("ns_suiteql_query", {"query": "SELECT 1"})

        assert result == {"n": 1}
        assert mock_post.call_count == 2
        mock_init.assert_called_once()

    @patch("mcp_client._initialize")
    @patch("mcp_client.requests.post")
    def test_write_tool_does_not_retry(self, mock_post, mock_init):
        mock_post.return_value = _sse_response([])

        with patch("mcp_client.uuid.uuid4", return_value="x"):
            with pytest.raises(MCPNoResponseError):
                mcp_client.call_tool("sf_create_record", {"object_name": "Task"})

        assert mock_post.call_count == 1
        mock_init.assert_not_called()
