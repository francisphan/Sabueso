"""Thin HTTP client for calling MCP server tools via JSON-RPC over SSE."""

from __future__ import annotations

import json
import logging
import os
import uuid

import requests

log = logging.getLogger(__name__)

MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8000")
MCP_TOKEN = os.getenv("MCP_WRITE_TOKEN", "")
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "30"))


def call_tool(tool_name: str, arguments: dict) -> dict | list | str:
    """Call an MCP tool and return the parsed result.

    Sends a JSON-RPC 2.0 request to the MCP server's SSE endpoint,
    parses the event stream for the result, and returns it.

    Raises ValueError on protocol errors, requests.RequestException on
    network errors.
    """
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

    headers = {
        "Content-Type": "application/json",
    }
    if MCP_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_TOKEN}"

    log.info("MCP call: %s(%s)", tool_name, arguments)

    resp = requests.post(
        f"{MCP_BASE_URL}/sse",
        json=payload,
        headers=headers,
        timeout=MCP_TIMEOUT,
        stream=True,
    )
    resp.raise_for_status()

    # Parse SSE stream for the JSON-RPC response
    result = _parse_sse_response(resp, request_id)
    log.info("MCP result for %s: %d chars", tool_name, len(str(result)))
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
            # Empty line = end of event, try to parse accumulated data
            raw = "\n".join(data_buffer)
            data_buffer.clear()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Look for our JSON-RPC response
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    err = message["error"]
                    raise ValueError(f"MCP error {err.get('code')}: {err.get('message')}")

                result = message.get("result", {})
                return _extract_content(result)

    # If we get here, check if any remaining data
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
    """Extract the actual data from MCP's content wrapper.

    MCP tool results come wrapped as:
    {"content": [{"type": "text", "text": "...json..."}]}
    """
    content = result.get("content", [])
    if not content:
        return result

    # MCP tools typically return a single text content block with JSON
    texts = [block.get("text", "") for block in content if block.get("type") == "text"]
    if not texts:
        return result

    combined = "\n".join(texts)

    # Try to parse as JSON (most tools return JSON-serialized dicts/lists)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined
