"""HTTP client for calling MCP server tools via streamable-http transport."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid

import requests

log = logging.getLogger(__name__)

MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8000")
MCP_TOKEN = os.getenv("MCP_WRITE_TOKEN", "")
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "30"))

_session_id: str | None = None
_session_lock = threading.Lock()


def _headers() -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if MCP_TOKEN:
        h["Authorization"] = f"Bearer {MCP_TOKEN}"
    if _session_id:
        h["Mcp-Session-Id"] = _session_id
    return h


def _endpoint() -> str:
    return f"{MCP_BASE_URL}/mcp"


def _initialize() -> None:
    """Perform the MCP initialize handshake and store the session ID.

    Raises on failure so callers can handle gracefully.
    """
    global _session_id

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "sabueso", "version": "0.1.0"},
        },
    }

    try:
        resp = requests.post(
            _endpoint(),
            json=payload,
            headers=_headers(),
            timeout=MCP_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("MCP initialization failed: %s", exc)
        _session_id = None
        raise ConnectionError(f"MCP server unavailable: {exc}") from exc

    _session_id = resp.headers.get("mcp-session-id")
    log.info("MCP session initialized: %s", _session_id)

    # Consume the response stream
    for _ in resp.iter_lines():
        pass

    # Send initialized notification
    notif = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    try:
        resp2 = requests.post(
            _endpoint(),
            json=notif,
            headers=_headers(),
            timeout=MCP_TIMEOUT,
        )
        # Notification may return 202 or 200, both are fine
        log.info("MCP initialized notification sent (status %d)", resp2.status_code)
    except requests.RequestException:
        log.warning("MCP initialized notification failed (non-fatal)")


def call_tool(tool_name: str, arguments: dict) -> dict | list | str:
    """Call an MCP tool and return the parsed result."""
    global _session_id

    # Initialize session if needed (thread-safe)
    with _session_lock:
        if _session_id is None:
            _initialize()

    request_id = str(uuid.uuid4())

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    # Arguments can carry guest PII — log at DEBUG, keep tool name at INFO.
    log.info("MCP call: %s", tool_name)
    log.debug("MCP call args: %s(%s)", tool_name, arguments)

    resp = requests.post(
        _endpoint(),
        json=payload,
        headers=_headers(),
        timeout=MCP_TIMEOUT,
        stream=True,
    )

    # Session expired — re-initialize and retry once (thread-safe)
    if resp.status_code in (400, 404):
        log.warning("MCP session expired, re-initializing...")
        with _session_lock:
            _session_id = None
            _initialize()
        resp = requests.post(
            _endpoint(),
            json=payload,
            headers=_headers(),
            timeout=MCP_TIMEOUT,
            stream=True,
        )

    resp.raise_for_status()

    result = _parse_sse_response(resp, request_id)
    # Result bodies carry guest PII / financials — keep the body at DEBUG.
    log.info("MCP result for %s (%d chars)", tool_name, len(str(result)))
    log.debug("MCP result body for %s: %.500s", tool_name, str(result))
    return result


def _parse_sse_response(resp: requests.Response, request_id: str):
    """Parse an SSE event stream and extract the JSON-RPC result."""
    data_buffer = []

    for line in resp.iter_lines(decode_unicode=True):
        if line is None:
            continue

        if line.startswith("data: "):
            data_buffer.append(line[6:])
        elif line == "" and data_buffer:
            raw = "\n".join(data_buffer)
            data_buffer.clear()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    err = message["error"]
                    raise ValueError(f"MCP error {err.get('code')}: {err.get('message')}")
                return _extract_content(message.get("result", {}))

    if data_buffer:
        raw = "\n".join(data_buffer)
        try:
            message = json.loads(raw)
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    err = message["error"]
                    raise ValueError(f"MCP error {err.get('code')}: {err.get('message')}")
                return _extract_content(message.get("result", {}))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No response received for request {request_id}")


def _extract_content(result: dict):
    """Extract data from MCP's content wrapper.

    MCP tools may return:
    - A single JSON object/array as text
    - Multiple JSON objects concatenated (one per record), each possibly
      pretty-printed across multiple lines
    - Plain text
    """
    content = result.get("content", [])
    if not content:
        return result

    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    if not texts:
        return result

    combined = "\n".join(texts)

    # Try parsing as a single JSON value first.
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        pass

    # Multi-record case: scan the combined text with raw_decode so we can
    # peel off one JSON value at a time even when each is pretty-printed
    # across many lines (which a naive split("\n") can't handle).
    objects = _decode_concatenated_json(combined)
    if len(objects) > 1:
        return objects
    if len(objects) == 1:
        return objects[0]
    return combined


def _decode_concatenated_json(s: str) -> list:
    """Return all top-level JSON values found in *s*, in order.

    Stops at the first thing that doesn't decode — returns whatever it
    collected up to that point.
    """
    decoder = json.JSONDecoder()
    s = s.strip()
    objects: list = []
    while s:
        try:
            value, end = decoder.raw_decode(s)
        except json.JSONDecodeError:
            break
        objects.append(value)
        s = s[end:].lstrip()
    return objects
