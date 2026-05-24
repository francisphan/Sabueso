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
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from tools_catalog import TOOLS, WRITE_OPERATIONS, OPPORTUNITY_PRODUCTS, TOUCH_SUBJECTS

log = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    """A write tool the agent wants to run, pending user confirmation."""

    tool_name: str
    arguments: dict
    summary: str  # Slack mrkdwn, rendered into the Block Kit card


@dataclass
class AgentResult:
    """Outcome of a single agentic run — text plus per-turn telemetry."""

    text: str
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0           # uncached portion only
    output_tokens: int = 0
    cache_read_tokens: int = 0      # served from prompt cache
    cache_creation_tokens: int = 0  # written to prompt cache (~1.25x cost)
    max_steps_hit: bool = False
    blocked_writes: int = 0
    error_type: str | None = None
    # Set when the loop halted on a write tool; handlers turns this into a
    # registered pending_op and posts a confirmation card instead of `text`.
    pending_confirmation: PendingConfirmation | None = None

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

## System ownership (use this to pick where to look)
- **Salesforce** = identity & relationship. Who someone is, who owns them,
  reservations (TVRS_Guest__c), open opportunities. Default for "tell me about X".
- **NetSuite** = money & inventory. Invoices, sales orders, payments, balances,
  items purchased, vineyard/owner custom fields. Go here for financial questions.
- **Pardot** = marketing engagement. Form fills, email opens, list memberships,
  visitor activity. Go here for "did they engage with campaign X".

## Tool selection
- For a "360 profile" or "full guest profile", use guest_360_profile.
- For a quick cross-system lookup by email, use lookup_guest_by_email.
- For invoices, sales orders, or financial records → NetSuite (ns_suiteql_query).
- For contacts, accounts, opportunities, guest reservations → Salesforce (sf_soql_query).
- For prospects, marketing lists, or visitor activity → Pardot tools.
- If the request is genuinely ambiguous (which person? which system?), ask.
  Otherwise take a reasonable first attempt — a partial answer beats a refusal.
- For follow-ups, use conversation history context.
- When you need full details on a NetSuite record, use ns_rest_get with the
  record type and ID — this returns ALL fields including custom ones.

## Wine label lookup (sommelier use case)
Users (especially the restaurant sommelier) may send a PHOTO of a wine bottle
label and ask whose wine it is. Almost every wine poured at the restaurant is
made on-property for a private owner, and the owner is identified by the
proprietary BRAND NAME on the label.

When you receive a wine-label image:
1. Read the label: the brand/wine name (the proprietary name, e.g. "Dos Abogados"),
   plus varietal and vintage if visible. The brand is the big proprietary name,
   NOT the grape ("Malbec") or "The Vines of Mendoza".
2. Call `wine_owner_lookup` with that text as label_text. It returns the owner's
   identity & contact, their wines + remaining stock, and (best effort) whether
   they're currently in-house. ONE call gets everything — prefer it over manual
   ns_/sf_ queries for this use case.
3. If match_status is "ambiguous", present the alternates and ask which brand.
   If "low_confidence", give the best-guess owner but say you're not certain and
   ask the user to confirm (don't state ownership as fact). If "not_found", tell
   the user it may be a third-party wine or ask for a clearer read of the brand.
4. Salesforce is sometimes unavailable; wine_owner_lookup still returns the
   NetSuite owner + inventory. Report what you have and note if enrichment
   (stay history / in-house status) was unavailable — don't fail the whole answer.

## Guest lookup strategy (IMPORTANT)
When looking up a person/guest, the starting point is ALWAYS Contact or Account
(Person Account), NOT TVRS_Guest__c.

If you only have a NAME (no email):
- Use sf_search (SOSL) — it scans Contact, Account, Lead, and TVRS_Guest__c
  in one call and catches Person Accounts (whose name lives on Account.Name,
  NOT on Contact.FirstName/LastName — a SOQL on Contact will miss them).
- Example: sf_search(search_str="FIND {Andrew Caplan} IN ALL FIELDS RETURNING
    Contact(Id, FirstName, LastName, Email, AccountId),
    Account(Id, Name, PersonEmail, IsPersonAccount, Description),
    Lead(Id, FirstName, LastName, Email, Company)")
- Then fan out to Account/Opportunity/TVRS_Guest__c as needed.

If you have an EMAIL:
1. Find the Contact by email: SELECT Id, FirstName, LastName, Email, AccountId
   FROM Contact WHERE Email = '...'
2. Get the Account details: SELECT Id, Name, PersonEmail, PersonTitle, Website,
   Description FROM Account WHERE Id = '...'
3. If the user wants reservation history, THEN query TVRS_Guest__c by Contact__c
4. If the user wants sales/opportunities, query Opportunity by AccountId

Only query TVRS_Guest__c directly when the user specifically asks about
reservations, check-ins, villas, or stay history.

## Sales-rep mode (write operations)
When a sales rep describes a new prospect interaction, your job is to either:
  (a) create a new opportunity → sf_create_opportunity_for_person, or
  (b) log a touch on an existing opportunity → sf_log_touch.

Decide based on whether the rep treats this as a fresh prospect ("just met
them at the pool") versus a follow-up ("talked to them again, here are notes").

Map casual language to the product enum:
  "vineyard / private vineyard / lot"            -> TVOM Vineyards
  "housing lot / 6 acres / build a home / land"  -> TVOM Housing Lot
  "villa expansion / second villa / expand"      -> TVOM Villa Expansion
  "membership / TVG / join the club"             -> TVG Membership

Map casual language to a touch subject:
  phone call / called / rang        -> Call
  in-person / casual / passerby     -> Conversation
  meeting / lunch / sit-down        -> Meetings
  text / WhatsApp / SMS             -> Text Message
  voicemail / left a message / LM   -> Voice Mail
  vineyard tour / walked the lot    -> Vineyard Visit

If a tool returns status "needs_person_details" or "ambiguous_person", surface
the result message to the rep verbatim — DO NOT guess which person they meant.
Same for "ambiguous_opp" on sf_log_touch.

Pass lead_source ONLY if the rep mentions where the lead came from (referral,
stayed at TVRS, friend, etc.). Don't invent it.

Writes never execute directly: when you call one of these tools, the bot
posts a confirmation card with Confirm/Cancel buttons to the rep. Do not
chain additional tool calls after a write — the loop halts at the write.

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
  IMPORTANT: For Person Accounts the full name lives on `Name` (e.g.
  "Andrew Caplan"), NOT on FirstName/LastName. Searching Contact by name
  will MISS people who only exist as a Person Account — use sf_search instead.
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
def _build_user_content(message: str, images: list[dict] | None) -> Any:
    """Build the user-turn content: a plain string, or text+image blocks.

    ``images`` is a list of {"media_type", "data"(base64)} dicts. When present we
    must send a content-block list; a short caption is synthesized for image-only
    messages so Claude knows what to do with the picture.
    """
    if not images:
        return message
    blocks: list[dict[str, Any]] = []
    caption = message.strip() or (
        "Here is a photo of a wine bottle label. Identify the wine and look up "
        "its owner."
    )
    blocks.append({"type": "text", "text": caption})
    for img in images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            }
        )
    return blocks


def run_agent(
    message: str,
    tool_executor: Callable[[str, dict], Any],
    conversation_history: list[dict[str, Any]] | None = None,
    images: list[dict] | None = None,
) -> AgentResult:
    """Run the agentic tool-use loop and return the final response + telemetry."""
    client = _get_client()

    messages: list[dict[str, Any]] = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": _build_user_content(message, images)})

    result = AgentResult(text="")

    # Cache the system prompt; tools render before system, so this also
    # caches the (much larger) TOOLS array across every step of the loop
    # and across subsequent turns within the 5-minute ephemeral TTL.
    cached_system = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    log.info("=== AGENT START === user_message=%r history_len=%d", message, len(messages) - 1)

    response = None
    for step in range(MAX_STEPS):
        result.steps = step + 1
        try:
            log.info("--- Step %d: sending %d messages to Claude (%s) ---", step, len(messages), MODEL)
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=cached_system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as exc:
            log.exception("Claude API error at step %d", step)
            result.error_type = type(exc).__name__
            result.text = f"Sorry, I hit a snag: `{exc}`"
            return result

        usage = response.usage
        result.input_tokens += usage.input_tokens
        result.output_tokens += usage.output_tokens
        result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        result.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        for i, block in enumerate(response.content):
            if block.type == "text":
                log.info("  Step %d block[%d] TEXT: %s", step, i, block.text[:1000])
            elif block.type == "tool_use":
                log.info("  Step %d block[%d] TOOL_USE: %s(%s)", step, i, block.name, json.dumps(block.input, default=str)[:500])
        log.info("  Step %d stop_reason=%s usage=%s", step, response.stop_reason, {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0),
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
        })

        if response.stop_reason == "end_turn":
            result.text = _extract_text(response)
            log.info("=== AGENT DONE === steps=%d final_len=%d", result.steps, len(result.text))
            return result

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            result.text = _extract_text(response)
            log.info("=== AGENT DONE (no tools) === steps=%d final_len=%d", result.steps, len(result.text))
            return result

        # Halt the loop if any tool in this batch is a write — surface as a
        # pending confirmation. The action handler executes the write after
        # the rep taps Confirm. We take the first write in the batch; mixed
        # read+write batches are pathological and just log a warning.
        write_call = next((b for b in tool_calls if b.name in WRITE_OPERATIONS), None)
        if write_call is not None:
            if len(tool_calls) > 1:
                log.warning(
                    "  Mixed batch with %d tool_calls including write %s — halting on the write",
                    len(tool_calls), write_call.name,
                )
            arguments = write_call.input or {}
            result.tool_calls.append(write_call.name)
            result.blocked_writes += 1
            result.pending_confirmation = PendingConfirmation(
                tool_name=write_call.name,
                arguments=arguments,
                summary=_summarize_pending(write_call.name, arguments),
            )
            log.info("=== AGENT HALTED ON WRITE === tool=%s steps=%d", write_call.name, result.steps)
            return result

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_call in tool_calls:
            tool_name = tool_call.name
            arguments = tool_call.input or {}
            result.tool_calls.append(tool_name)

            log.info("  Executing tool: %s(%s)", tool_name, json.dumps(arguments, default=str))

            try:
                tool_output = tool_executor(tool_name, arguments)
                result_str = json.dumps(tool_output, default=str)
                if len(result_str) > 80_000:
                    result_str = result_str[:80_000] + "\n... [truncated]"
                log.info("  Tool %s returned %d chars: %.1000s", tool_name, len(result_str), result_str)
            except Exception as e:
                log.error("  Tool %s FAILED: %s", tool_name, e, exc_info=True)
                err_type = type(e).__name__
                result_str = json.dumps({
                    "error": f"Tool {tool_name} failed ({err_type}). "
                             "The error has been logged for investigation."
                })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    result.max_steps_hit = True
    log.warning("=== AGENT HIT MAX STEPS (%d) ===", MAX_STEPS)
    result.text = (
        _extract_text(response) if response is not None else ""
    ) or "I got a bit lost sniffing around. Could you try a simpler question?"
    return result


def _extract_text(response: anthropic.types.Message) -> str:
    """Extract text content from a Claude response."""
    parts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(parts).strip() or "I couldn't find anything to report."


def _escape_mrkdwn(s: str) -> str:
    """Defang Slack mrkdwn meta-characters in LLM/rep-controlled text.

    Escaping `<` blocks: channel pings (`<!here>`, `<!channel>`, `<!everyone>`),
    user pings (`<@U123>`), and link spoofing (`<http://evil|Salesforce>`).
    The `&` escape keeps `<` rendering as the literal `<` glyph (`&lt;`).
    """
    if not isinstance(s, str):
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _summarize_pending(tool_name: str, arguments: dict) -> str:
    """Render a Slack mrkdwn summary of a pending write for the confirmation card.

    LLM/rep-controlled fields are passed through _escape_mrkdwn so a crafted
    `notes` value (e.g. `<!channel>`) can't ping the whole channel.
    """
    if tool_name == "sf_create_opportunity_for_person":
        person = _escape_mrkdwn(arguments.get("person_hint", "?"))
        product = _escape_mrkdwn(arguments.get("product", "?"))  # enum-validated, but escape anyway
        notes = _escape_mrkdwn(arguments.get("notes", ""))
        lead_source = _escape_mrkdwn(arguments.get("lead_source", ""))
        touches = arguments.get("touches") or []
        touch_str = ", ".join(_escape_mrkdwn(t.get("subject", "?")) for t in touches) if touches else "none"
        lines = [
            f"*Create opportunity:* `{product}` for *{person}*",
            f"• Touches: {touch_str}",
        ]
        if lead_source:
            lines.append(f"• Lead source: {lead_source}")
        if notes:
            lines.append(f"• Notes: {notes}")
        return "\n".join(lines)

    if tool_name == "sf_log_touch":
        target = _escape_mrkdwn(arguments.get("opp_hint") or arguments.get("person_hint") or "?")
        subject = _escape_mrkdwn(arguments.get("subject", "?"))
        notes = _escape_mrkdwn(arguments.get("notes", ""))
        when = _escape_mrkdwn(arguments.get("when", "today"))
        lines = [
            f"*Log touch* on opportunity for *{target}*",
            f"• Subject: {subject}",
            f"• When: {when}",
        ]
        if notes:
            lines.append(f"• Notes: {notes}")
        return "\n".join(lines)

    # Fallback for any future write tool — dump escaped args.
    return f"Execute `{tool_name}` with `{_escape_mrkdwn(json.dumps(arguments, default=str)[:300])}`"


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
