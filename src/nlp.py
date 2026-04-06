"""
NLP integration layer -- translates natural-language Slack messages into
structured MCP-server function calls via the Claude API (tool_use).

Public API
----------
    parse_request(message, conversation_history) -> ParsedIntent
    format_response(raw_data, original_request)  -> str
    build_tool_result_messages(intent, result)    -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from tools_catalog import TOOLS, WRITE_OPERATIONS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024
SUMMARY_MAX_TOKENS = 2048

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are Sabueso, a data bloodhound embedded in Slack for a hospitality company
(The Vines of Mendoza). Your job is to sniff out the information users need by
calling the correct back-end function using the tools provided. You're friendly,
efficient, and good at tracking things down — but you keep responses concise
and professional. A little personality is fine, but don't overdo it.

Guidelines:
- Always prefer the most specific tool.  For example, if the user asks for a
  "360 profile" or "full guest profile", use guest_360_profile.  For a quick
  lookup, use lookup_guest_by_email.
- When the user asks about invoices, sales orders, or financial records,
  prefer NetSuite (ns_suiteql_query or ns_rest_list).
- When the user asks about contacts, accounts, or opportunities, prefer
  Salesforce (sf_soql_query).
- When the user asks about prospects, marketing lists, or visitor activity,
  prefer Pardot tools.
- Generate valid SOQL / SuiteQL when using the query tools.  Use correct field
  names and date literals.
- If the user's request is ambiguous or you need more information, respond with
  a plain text message asking for clarification instead of calling a tool.
- For follow-up messages that refine a previous query, incorporate the context
  from the conversation history.
- Today's date can be inferred from the conversation metadata if needed.
"""

SUMMARY_SYSTEM_PROMPT = """\
You are Sabueso, formatting data you just dug up into clear Slack messages.
Follow these rules:
- Use Slack mrkdwn formatting (*bold*, _italic_, `code`).
- Summarise large result sets -- show counts and highlight key rows.
- For a single record, show the important fields in a readable layout.
- Keep responses concise; users are on mobile or in a busy Slack channel.
- If the data is empty, say so clearly (e.g. "Couldn't find a trail on that one.").
- Never fabricate data -- only use what is provided.
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class ParsedIntent:
    """Structured result returned by ``parse_request``."""

    tool_name: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    is_write_operation: bool = False
    confidence: float = 1.0
    clarification_needed: str | None = None
    raw_text_response: str | None = None


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Conversation-history helpers
# ---------------------------------------------------------------------------
def _build_messages(
    message: str,
    conversation_history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": message})
    return messages


# ---------------------------------------------------------------------------
# Core: parse_request
# ---------------------------------------------------------------------------
def parse_request(
    message: str,
    conversation_history: list[dict[str, Any]] | None = None,
) -> ParsedIntent:
    """Interpret a natural-language message and return a ParsedIntent."""
    client = _get_client()
    messages = _build_messages(message, conversation_history)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
    except anthropic.APIError as exc:
        log.exception("Claude API error during parse_request")
        return ParsedIntent(
            clarification_needed=(
                "Sorry, I ran into a problem understanding your request. "
                "Please try again in a moment."
            ),
            confidence=0.0,
            raw_text_response=f"API error: {exc}",
        )

    return _extract_intent(response)


def _extract_intent(response: anthropic.types.Message) -> ParsedIntent:
    tool_use_block = None
    text_parts: list[str] = []

    for block in response.content:
        if block.type == "tool_use":
            tool_use_block = block
        elif block.type == "text":
            text_parts.append(block.text)

    if tool_use_block is not None:
        tool_name: str = tool_use_block.name
        parameters: dict[str, Any] = tool_use_block.input or {}
        is_write = tool_name in WRITE_OPERATIONS

        return ParsedIntent(
            tool_name=tool_name,
            parameters=parameters,
            is_write_operation=is_write,
            confidence=1.0,
        )

    combined_text = "\n".join(text_parts).strip()

    if combined_text.endswith("?"):
        return ParsedIntent(clarification_needed=combined_text, confidence=0.0)

    return ParsedIntent(raw_text_response=combined_text, confidence=0.0)


# ---------------------------------------------------------------------------
# Core: format_response
# ---------------------------------------------------------------------------
def format_response(
    raw_data: dict[str, Any] | list | str,
    original_request: str,
) -> str:
    """Use Claude to summarise raw_data into a human-friendly Slack message."""
    client = _get_client()

    data_str = json.dumps(raw_data, default=str)
    if len(data_str) > 80_000:
        data_str = data_str[:80_000] + "\n... [truncated]"

    user_content = (
        f"The user asked: \"{original_request}\"\n\n"
        f"Here is the raw data returned by the backend:\n```json\n{data_str}\n```\n\n"
        "Please summarise this data into a clear, concise Slack message."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError:
        log.exception("Claude API error during format_response")
        preview = data_str[:3000]
        return f"Here's the raw result:\n```\n{preview}\n```"

    return "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


# ---------------------------------------------------------------------------
# Convenience: build conversation history entries after a tool call
# ---------------------------------------------------------------------------
def build_tool_result_messages(
    intent: ParsedIntent,
    result: dict[str, Any] | list | str,
) -> list[dict[str, Any]]:
    """Return message dicts recording a completed tool call for conversation history."""
    if intent.tool_name is None:
        return [
            {
                "role": "assistant",
                "content": intent.raw_text_response or intent.clarification_needed or "",
            }
        ]

    tool_use_id = f"call_{intent.tool_name}"

    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": intent.tool_name,
                    "input": intent.parameters,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result, default=str),
                }
            ],
        },
    ]
