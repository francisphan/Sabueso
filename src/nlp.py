"""
NLP integration layer — agentic tool-use loop.

Translates natural-language Slack messages into MCP tool calls via
Claude API. Supports multi-step reasoning: Claude can chain multiple
tool calls before producing a final answer.

Public API
----------
    run_agent(message, tool_executor, conversation_history) -> str
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

import anthropic

from tools_catalog import TOOLS, WRITE_OPERATIONS

log = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 4096
MAX_STEPS = 8  # safety limit on tool-call iterations

SYSTEM_PROMPT = """\
You are Sabueso, a data bloodhound embedded in Slack for The Vines of Mendoza,
a luxury private residence and wine hotel in Mendoza, Argentina. Your job is to
sniff out the information users need by calling the correct back-end function
using the tools provided. You're friendly, efficient, and good at tracking
things down — but you keep responses concise and professional.

You can chain multiple tool calls to answer complex questions. For example,
first look up a customer, then fetch their transactions, then get details on
a specific record. Keep going until you have enough information to give the
user a complete answer.

## Formatting rules for your final response
- Use Slack mrkdwn formatting (*bold*, _italic_, `code`).
- NEVER use markdown tables (| --- | syntax) — Slack does not render them.
  For tabular data, use a code block with aligned columns.
  For small results (1-3 records), use bullet points with bold labels.
- Keep responses concise; users are on mobile or in a busy Slack channel.
- If the data is empty, say so clearly.
- Never fabricate data — only use what is provided.

## Tool selection
- For a "360 profile" or "full guest profile", use guest_360_profile.
- For a quick cross-system lookup by email, use lookup_guest_by_email.
- For invoices, sales orders, or financial records → NetSuite (ns_suiteql_query).
- For contacts, accounts, opportunities, guest reservations → Salesforce (sf_soql_query).
- For prospects, marketing lists, or visitor activity → Pardot tools.
- If the request is ambiguous, ask for clarification instead of calling a tool.
- For follow-ups, use conversation history context.
- When you need full details on a NetSuite record, use ns_rest_get with the
  record type and ID — this returns ALL fields including custom ones.

## Guest lookup strategy (IMPORTANT)
When looking up a person/guest, the starting point is ALWAYS Contact or Account
(Person Account), NOT TVRS_Guest__c. The typical approach:
1. Find the Contact by email: SELECT Id, FirstName, LastName, Email, AccountId
   FROM Contact WHERE Email = '...'
2. Get the Account details: SELECT Id, Name, PersonEmail, PersonTitle, Website,
   Description FROM Account WHERE Id = '...'
3. If the user wants reservation history, THEN query TVRS_Guest__c by Contact__c
4. If the user wants sales/opportunities, query Opportunity by AccountId

Only query TVRS_Guest__c directly when the user specifically asks about
reservations, check-ins, villas, or stay history.

## Salesforce schema (use these exact field names in SOQL)

TVRS_Guest__c (guest reservations):
  Id, Email__c (external ID), Guest_First_Name__c, Guest_Last_Name__c,
  Check_In_Date__c, Check_Out_Date__c, Villa_number__c, City__c,
  State_Province__c, Country__c, Language__c, Telephone__c, Comments__c,
  Contact__c (lookup to Contact), Contact__r.AccountId
  Booleans: Future_Sales_Prospect__c, TVG__c, Greeted_at_Check_In__c, etc.
  Example: SELECT Id, Guest_First_Name__c, Guest_Last_Name__c, Email__c,
    Check_In_Date__c, Check_Out_Date__c, Villa_number__c
    FROM TVRS_Guest__c WHERE Check_In_Date__c >= TODAY ORDER BY Check_In_Date__c ASC

Account (Person Accounts — IsPersonAccount = true):
  Id, Name, PersonEmail, PersonTitle, Website, Description, IsPersonAccount,
  Primary_Language__pc, OwnerId, CreatedDate, LastModifiedDate
  Example: SELECT Id, Name, PersonEmail, Description FROM Account
    WHERE IsPersonAccount = true LIMIT 10

Contact:
  Id, FirstName, LastName, Email, Phone, AccountId, Has_TVRS_Guest_Record__c,
  CreatedDate, LastModifiedDate
  Traverse to Account: Contact.Account.Name
  Example: SELECT Id, FirstName, LastName, Email, AccountId FROM Contact
    WHERE Email = 'someone@example.com'

Opportunity:
  Id, Name, StageName, Amount, CloseDate, IsClosed, IsWon, AccountId, OwnerId,
  Owner.Name, Account.Name, CreatedDate, LastModifiedDate
  Example: SELECT Id, Name, StageName, Amount, CloseDate, Account.Name
    FROM Opportunity WHERE IsClosed = false ORDER BY CloseDate ASC

Lead:
  Id, FirstName, LastName, Email, Phone, Company, Status, IsConverted,
  ConvertedContactId, ConvertedAccountId

Campaign / CampaignMember:
  Campaign: Id, Name, Type, Status, StartDate, EndDate
  CampaignMember: Id, CampaignId, ContactId, LeadId, Status

Task:
  Id, Subject, Status, WhatId, WhoId, ActivityDate, Description, OwnerId

## NetSuite schema (use these exact field/table names in SuiteQL)

customer table:
  id, entityid, companyname, firstname, lastname, email, phone, isperson,
  datecreated, lastmodifieddate, externalid, balance, subsidiary, salesrep
  IMPORTANT: Many customers have isPerson=false and name stored in companyname
  (format "Lastname, Firstname"), NOT in firstname/lastname fields. When
  searching by name, ALWAYS search companyname too:
    WHERE LOWER(companyname) LIKE '%name%' OR LOWER(lastname) = 'name'
  Custom fields (vineyard/owner-specific):
    custentity10 = vineyard/lot number
    custentity_vom_certificateunitnumber = certificate unit numbers
    custentity_vom_ownercode = owner code (e.g. "RITS")
    custentity_vom_winebrandname = wine brand name
    custentity_vom_nickname = nickname
    custentity_vom_numberoflots = number of lots
    custentity_vom_csam = assigned CSAM (employee reference)
    custentity_vom_clientwineprofile = wine profile/preferences
    custentity_vom_happinesslevel = happiness level (reference)
    custentity_ce_ownercode = owner code
    custentity_tek_block = vineyard block info (JSON)
    custentity37 = wine brand/label name
    comments = internal notes about the customer
  Example: SELECT id, entityid, companyname, email, custentity10,
    custentity_vom_ownercode FROM customer
    WHERE LOWER(companyname) LIKE '%rittvo%'
  To get ALL fields for a customer, use ns_rest_get(record_type="customer", record_id="...")

transaction table (unified — filter by type):
  id, tranid, type, entity, trandate, status, foreigntotal, memo, subsidiary,
  duedate, foreignamountremaining, createddate, lastmodifieddate
  Type codes: SalesOrd, CustInvc, CustPymt, CustCred, VendBill, PurchOrd, Journal
  Example (invoices): SELECT t.id, t.tranid, t.trandate, t.status,
    t.foreigntotal, c.companyname FROM transaction t
    JOIN customer c ON t.entity = c.id WHERE t.type = 'CustInvc'
    ORDER BY t.trandate DESC
  Example (sales orders): same but t.type = 'SalesOrd'

transactionline table (line items — join to transaction):
  id, transaction, item, quantity, rate, amount
  Example: SELECT tl.item, i.itemid, tl.quantity, tl.rate, tl.amount
    FROM transactionline tl JOIN item i ON tl.item = i.id
    WHERE tl.transaction = 12345

item table:
  id, itemid, displayname, description, type, baseprice, quantityavailable,
  quantityonorder, isinactive
  Types: InvtPart, NonInvtPart, Service

vendor table:
  id, entityid, companyname, email, phone, balance, isinactive

employee table:
  id, entityid, firstname, lastname, email, title, isinactive
"""


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
# Agentic loop
# ---------------------------------------------------------------------------
def run_agent(
    message: str,
    tool_executor: Callable[[str, dict], Any],
    conversation_history: list[dict[str, Any]] | None = None,
) -> str:
    """Run the agentic tool-use loop and return the final text response.

    Parameters
    ----------
    message:
        The user's natural-language message.
    tool_executor:
        A callable(tool_name, arguments) -> result that executes MCP tools.
    conversation_history:
        Optional prior messages for multi-turn context.

    Returns
    -------
    str
        The final Slack-formatted response text.
    """
    client = _get_client()

    messages: list[dict[str, Any]] = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": message})

    log.info("=== AGENT START === user_message=%r history_len=%d", message, len(messages) - 1)

    for step in range(MAX_STEPS):
        try:
            log.info("--- Step %d: sending %d messages to Claude (%s) ---", step, len(messages), MODEL)
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as exc:
            log.exception("Claude API error at step %d", step)
            return f"Sorry, I hit a snag: `{exc}`"

        # Log all content blocks
        for i, block in enumerate(response.content):
            if block.type == "text":
                log.info("  Step %d block[%d] TEXT: %s", step, i, block.text[:1000])
            elif block.type == "tool_use":
                log.info("  Step %d block[%d] TOOL_USE: %s(%s)", step, i, block.name, json.dumps(block.input, default=str)[:500])
        log.info("  Step %d stop_reason=%s usage=%s", step, response.stop_reason,
                 {"input": response.usage.input_tokens, "output": response.usage.output_tokens})

        # If Claude is done (no more tool calls), extract the final text
        if response.stop_reason == "end_of_turn":
            final = _extract_text(response)
            log.info("=== AGENT DONE === steps=%d final_len=%d", step + 1, len(final))
            return final

        # Process tool calls
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            final = _extract_text(response)
            log.info("=== AGENT DONE (no tools) === steps=%d final_len=%d", step + 1, len(final))
            return final

        # Add assistant message with all content blocks
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool call and collect results
        tool_results = []
        for tool_call in tool_calls:
            tool_name = tool_call.name
            arguments = tool_call.input or {}
            is_write = tool_name in WRITE_OPERATIONS

            log.info("  Executing tool: %s(%s) write=%s", tool_name, json.dumps(arguments, default=str), is_write)

            if is_write:
                log.info("  BLOCKED — write operation requires confirmation")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": json.dumps({
                        "error": "Write operations require confirmation. "
                                 "Please confirm you want to proceed."
                    }),
                })
                continue

            try:
                result = tool_executor(tool_name, arguments)
                result_str = json.dumps(result, default=str)
                if len(result_str) > 80_000:
                    result_str = result_str[:80_000] + "\n... [truncated]"
                log.info("  Tool %s returned %d chars: %.1000s", tool_name, len(result_str), result_str)
            except Exception as e:
                log.error("  Tool %s FAILED: %s", tool_name, e, exc_info=True)
                result_str = json.dumps({"error": str(e)})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    # Hit the step limit
    log.warning("=== AGENT HIT MAX STEPS (%d) ===", MAX_STEPS)
    return _extract_text(response) or "I got a bit lost sniffing around. Could you try a simpler question?"


def _extract_text(response: anthropic.types.Message) -> str:
    """Extract text content from a Claude response."""
    parts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(parts).strip() or "I couldn't find anything to report."


# ---------------------------------------------------------------------------
# Conversation history helpers
# ---------------------------------------------------------------------------
def build_history_from_agent_run(
    user_message: str,
    final_response: str,
) -> list[dict[str, Any]]:
    """Build minimal conversation history entries for multi-turn context.

    We store just the user message and final assistant response (not
    intermediate tool calls) to keep history compact.
    """
    return [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": final_response},
    ]
