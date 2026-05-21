"""
High-level Salesforce intent functions for the Sabueso Slack bot.

Composes primitive MCP tools (sf_search, sf_soql_query, sf_create_record) into
two agent-dispatchable intents:

  create_opportunity_for_person — SOSL lookup → create Opportunity + Task(s).
  log_touch                     — find open Opportunity → create Task.

Neither function should be called directly by the LLM; the dispatcher injects
slack_user_id and slack_client before handing control here.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

import audit
import sf_identity
from mcp_client import call_tool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODUCTS: frozenset[str] = frozenset(
    {
        "TVOM Vineyards",
        "TVOM Housing Lot",
        "TVOM Villa Expansion",
        "TVG Membership",
    }
)

VALID_TOUCH_SUBJECTS: frozenset[str] = frozenset(
    {
        "Call",
        "Conversation",
        "Meetings",
        "Text Message",
        "Voice Mail",
        "Vineyard Visit",
        "Other",
    }
)

_SF_INSTANCE_URL = os.getenv("SF_INSTANCE_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _opp_url(opp_id: str) -> str:
    """Return a Lightning URL for the given Opportunity ID."""
    if _SF_INSTANCE_URL:
        return f"{_SF_INSTANCE_URL}/lightning/r/Opportunity/{opp_id}/view"
    return opp_id


def _sanitize_sosl(s: str) -> str:
    """Escape characters that have special meaning inside SOSL search terms."""
    # Order matters: escape backslash first, then braces/quotes.
    s = s.replace("\\", "\\\\")
    s = s.replace("{", "\\{")
    s = s.replace("}", "\\}")
    s = s.replace("'", "\\'")
    s = s.replace('"', '\\"')
    return s


def _records(result: Any) -> list[dict]:
    """Normalize a SOQL result envelope into a plain list of record dicts."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if "records" in result and isinstance(result["records"], list):
            return result["records"]
        inner = result.get("result")
        if isinstance(inner, list):
            return inner
    return []


def end_of_current_quarter(today: date) -> date:
    """Return the last day of the current quarter, rolling forward by one quarter
    if *today* is within 5 days of the quarter end.
    """
    quarter_ends = [
        date(today.year, 3, 31),
        date(today.year, 6, 30),
        date(today.year, 9, 30),
        date(today.year, 12, 31),
    ]
    for q_end in quarter_ends:
        if today <= q_end:
            if (q_end - today).days < 5:
                # Roll to next quarter's end
                if q_end.month == 12:
                    return date(today.year + 1, 3, 31)
                next_idx = quarter_ends.index(q_end) + 1
                return quarter_ends[next_idx]
            return q_end
    # Should never reach here — fallback to Dec 31 of this year
    return date(today.year, 12, 31)


def parse_when(s: str) -> date:
    """Parse an ISO date string or "today" into a date. Defaults to today on failure."""
    if not s or s.strip().lower() == "today":
        return date.today()
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        log.warning("Could not parse date %r — defaulting to today", s)
        return date.today()


def _is_sf_id(s: str) -> bool:
    """True if s looks like a Salesforce record ID (15 or 18 alphanumeric chars)."""
    return bool(s and re.fullmatch(r"[A-Za-z0-9]{15}|[A-Za-z0-9]{18}", s))


def _sosl_search_person(hint: str) -> list[dict]:
    """Run a SOSL search and collapse results into a normalised candidate list.

    Each candidate:
        {name, email, account_id, source}

    Source is "PersonAccount", "Contact", or "Lead".
    """
    sanitized = _sanitize_sosl(hint)
    sosl = (
        f"FIND {{{sanitized}}} IN ALL FIELDS "
        "RETURNING "
        "Contact(Id, FirstName, LastName, Email, AccountId, Account.Name), "
        "Account(Id, Name, PersonEmail, IsPersonAccount), "
        "Lead(Id, FirstName, LastName, Email, Company)"
    )
    result = call_tool("sf_search", {"search_str": sosl})

    # Normalise envelope: sf_search returns {"searchRecords": [...]} or a list.
    if isinstance(result, list):
        records = result
    elif isinstance(result, dict):
        records = result.get("searchRecords", result.get("records", []))
    else:
        records = []

    candidates: list[dict] = []
    seen_account_ids: set[str] = set()

    for rec in records:
        attr = rec.get("attributes", {})
        obj_type = attr.get("type", "") if isinstance(attr, dict) else ""

        if obj_type == "Account":
            # Only care about Person Accounts
            if not rec.get("IsPersonAccount"):
                continue
            acct_id = rec.get("Id")
            if acct_id in seen_account_ids:
                continue
            seen_account_ids.add(acct_id)
            candidates.append(
                {
                    "name": rec.get("Name", ""),
                    "email": rec.get("PersonEmail", ""),
                    "account_id": acct_id,
                    "source": "PersonAccount",
                }
            )

        elif obj_type == "Contact":
            acct_id = rec.get("AccountId")
            if acct_id and acct_id in seen_account_ids:
                continue
            if acct_id:
                seen_account_ids.add(acct_id)
            first = rec.get("FirstName") or ""
            last = rec.get("LastName") or ""
            # Prefer Account name if available
            acct_obj = rec.get("Account") or {}
            name = acct_obj.get("Name") or f"{first} {last}".strip() or rec.get("Id", "")
            candidates.append(
                {
                    "name": name,
                    "email": rec.get("Email", ""),
                    "account_id": acct_id,
                    "source": "Contact",
                }
            )

        elif obj_type == "Lead":
            first = rec.get("FirstName") or ""
            last = rec.get("LastName") or ""
            name = f"{first} {last}".strip() or rec.get("Id", "")
            candidates.append(
                {
                    "name": name,
                    "email": rec.get("Email", ""),
                    "account_id": None,  # Leads don't have AccountId until converted
                    "source": "Lead",
                    "lead_id": rec.get("Id"),
                }
            )

    return candidates


# ---------------------------------------------------------------------------
# Public intent: create_opportunity_for_person
# ---------------------------------------------------------------------------


def create_opportunity_for_person(
    *,
    slack_user_id: str,
    slack_client: Any,
    person_hint: str,
    product: str,
    notes: str | None = None,
    lead_source: str | None = None,
    touches: list[dict] | None = None,
) -> dict:
    """Look up a person, create an Opportunity owned by the rep, and log touches.

    Returns a status dict. The caller (dispatcher) is responsible for surfacing
    the result to the user.
    """
    touches = touches or []

    # 1. Validate product.
    if product not in PRODUCTS:
        return {"status": "invalid_product", "expected": sorted(PRODUCTS)}

    # 2. Validate touch subjects up-front.
    for touch in touches:
        subj = touch.get("subject", "")
        if subj not in VALID_TOUCH_SUBJECTS:
            return {"status": "invalid_subject", "subject": subj}

    # 3. Resolve person via SOSL.
    candidates = _sosl_search_person(person_hint)

    # 4 / 5 / 6. Handle 0, >1, or exactly 1 candidate.
    if not candidates:
        return {
            "status": "needs_person_details",
            "message": (
                "I don't see anyone matching. Can you give me their email or phone?"
            ),
        }

    if len(candidates) > 1:
        return {
            "status": "ambiguous_person",
            "candidates": [
                {
                    "name": c["name"],
                    "email": c.get("email", ""),
                    "account_id": c.get("account_id"),
                    "source": c["source"],
                }
                for c in candidates[:5]
            ],
        }

    person = candidates[0]
    account_id = person.get("account_id")
    account_name = person["name"]

    # 7. Resolve SF user identity.
    sf_user_id = sf_identity.get_sf_user_id_for_slack(slack_user_id, slack_client)
    if not sf_user_id:
        return {
            "status": "no_sf_identity",
            "message": (
                "I can't find a Salesforce user matching your Slack email. "
                "Ask an admin to run `!access map @you <sf_user_id>`."
            ),
        }

    # 8. Compute CloseDate.
    today = date.today()
    close_date = end_of_current_quarter(today)

    # 9. Build Opportunity payload.
    description_parts = []
    if notes:
        description_parts.append(notes)
    description_parts.append(
        f"Created via Sabueso by <@{slack_user_id}> on {today.isoformat()}"
    )
    description = "\n\n".join(description_parts)

    opp_data: dict[str, Any] = {
        "Name": f"{account_name} - {product}",
        "StageName": "Deep Discovery",
        "CloseDate": close_date.isoformat(),
        "OwnerId": sf_user_id,
        "Description": description,
    }
    if account_id:
        opp_data["AccountId"] = account_id
    if lead_source:
        opp_data["LeadSource"] = lead_source

    # 10. Create Opportunity.
    try:
        opp_result = call_tool("sf_create_record", {"object_name": "Opportunity", "data": opp_data})
    except Exception as exc:
        log.exception("Failed to create Opportunity for %s", account_name)
        return {"status": "create_failed", "error": str(exc)}

    if not isinstance(opp_result, dict) or not opp_result.get("id") or not opp_result.get("success", True):
        err = (opp_result.get("errors") or []) if isinstance(opp_result, dict) else []
        return {"status": "create_failed", "error": str(err or opp_result)}

    opp_id: str = opp_result["id"]

    # 11. Create Tasks for each touch.
    touch_results: list[dict] = []
    for touch in touches:
        subj = touch.get("subject", "")
        touch_notes = touch.get("notes", "") or ""
        touch_when = parse_when(touch.get("when", "today"))

        task_data: dict[str, Any] = {
            "WhatId": opp_id,
            "OwnerId": sf_user_id,
            "Subject": subj,
            "Description": touch_notes,
            "ActivityDate": touch_when.isoformat(),
            "Status": "Completed",
            "Priority": "Normal",
        }
        try:
            task_result = call_tool("sf_create_record", {"object_name": "Task", "data": task_data})
            task_id = task_result.get("id") if isinstance(task_result, dict) else None
            touch_results.append({"subject": subj, "ok": bool(task_id), "task_id": task_id})
        except Exception as exc:
            log.exception("Failed to create Task (subject=%r) on opp %s", subj, opp_id)
            touch_results.append({"subject": subj, "ok": False, "error": type(exc).__name__})

    # 12. Audit.
    try:
        audit.record_write(
            {
                "tool": "sf_create_opportunity_for_person",
                "slack_user": slack_user_id,
                "sf_user": sf_user_id,
                "opp_id": opp_id,
                "product": product,
                "person_account_id": account_id,
                "person_name": account_name,
                "touches": touch_results,
                "lead_source": lead_source,
            }
        )
    except Exception:
        log.exception("Audit write failed for opp %s (non-fatal)", opp_id)

    # 13. Return success.
    return {
        "status": "ok",
        "opp_id": opp_id,
        "opp_url": _opp_url(opp_id),
        "person_name": account_name,
        "touches": touch_results,
    }


# ---------------------------------------------------------------------------
# Public intent: log_touch
# ---------------------------------------------------------------------------


def log_touch(
    *,
    slack_user_id: str,
    slack_client: Any,
    opp_hint: str | None = None,
    person_hint: str | None = None,
    subject: str,
    notes: str = "",
    when: str = "today",
) -> dict:
    """Find an open Opportunity and log a Task touch on it.

    Resolution order:
      1. opp_hint looks like an SF ID → use directly.
      2. opp_hint provided → SOQL name LIKE search.
      3. person_hint provided → SOSL person lookup → SOQL opp by AccountId.
    """
    # 1. Validate subject.
    if subject not in VALID_TOUCH_SUBJECTS:
        return {"status": "invalid_subject", "subject": subject}

    # 2. Resolve target opportunity.
    opp_id: str | None = None
    opp_name: str = ""

    if opp_hint and _is_sf_id(opp_hint) and opp_hint.startswith("006"):
        # Direct ID reference — trust it.
        opp_id = opp_hint
        opp_name = opp_hint
    elif opp_hint:
        # Name fragment search.
        escaped = opp_hint.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"SELECT Id, Name, StageName, IsClosed, Account.Name "
            f"FROM Opportunity "
            f"WHERE Name LIKE '%{escaped}%' AND IsClosed = false "
            f"LIMIT 6"
        )
        try:
            result = call_tool("sf_soql_query", {"query_str": query})
        except Exception as exc:
            log.exception("SOQL opp search failed for hint %r", opp_hint)
            return {"status": "create_failed", "error": str(exc)}

        opps = _records(result)
        if not opps:
            return {
                "status": "no_open_opps",
                "message": f'No open opportunities found matching "{opp_hint}".',
            }
        if len(opps) > 1:
            return {
                "status": "ambiguous_opp",
                "candidates": [
                    {
                        "id": o.get("Id"),
                        "name": o.get("Name"),
                        "stage": o.get("StageName"),
                        "account_name": (o.get("Account") or {}).get("Name"),
                    }
                    for o in opps[:5]
                ],
            }
        opp_id = opps[0]["Id"]
        opp_name = opps[0].get("Name", opp_id)

    elif person_hint:
        # Resolve person first.
        candidates = _sosl_search_person(person_hint)
        if not candidates:
            return {
                "status": "no_open_opps",
                "message": (
                    f'I couldn\'t find anyone matching "{person_hint}" in Salesforce.'
                ),
            }
        if len(candidates) > 1:
            return {
                "status": "ambiguous_person",
                "candidates": [
                    {
                        "name": c["name"],
                        "email": c.get("email", ""),
                        "account_id": c.get("account_id"),
                        "source": c["source"],
                    }
                    for c in candidates[:5]
                ],
            }

        acct_id = candidates[0].get("account_id")
        if not acct_id:
            return {
                "status": "no_open_opps",
                "message": (
                    f"Found \"{candidates[0]['name']}\" as a Lead (not yet converted). "
                    "No Opportunities can be linked until the Lead is converted to a Contact/Account."
                ),
            }

        escaped_id = acct_id.replace("'", "\\'")
        query = (
            f"SELECT Id, Name, StageName, Account.Name "
            f"FROM Opportunity "
            f"WHERE AccountId = '{escaped_id}' AND IsClosed = false "
            f"LIMIT 6"
        )
        try:
            result = call_tool("sf_soql_query", {"query_str": query})
        except Exception as exc:
            log.exception("SOQL opp search failed for account %r", acct_id)
            return {"status": "create_failed", "error": str(exc)}

        opps = _records(result)
        person_name = candidates[0]["name"]
        if not opps:
            return {
                "status": "no_open_opps",
                "message": f"No open opportunities found for {person_name}.",
            }
        if len(opps) > 1:
            return {
                "status": "ambiguous_opp",
                "candidates": [
                    {
                        "id": o.get("Id"),
                        "name": o.get("Name"),
                        "stage": o.get("StageName"),
                        "account_name": (o.get("Account") or {}).get("Name"),
                    }
                    for o in opps[:5]
                ],
            }
        opp_id = opps[0]["Id"]
        opp_name = opps[0].get("Name", opp_id)

    else:
        return {
            "status": "no_open_opps",
            "message": "Please provide either an opportunity name/ID or a person's name.",
        }

    # 3. Resolve SF user identity.
    sf_user_id = sf_identity.get_sf_user_id_for_slack(slack_user_id, slack_client)
    if not sf_user_id:
        return {
            "status": "no_sf_identity",
            "message": (
                "I can't find a Salesforce user matching your Slack email. "
                "Ask an admin to run `!access map @you <sf_user_id>`."
            ),
        }

    # 4. Build Task payload.
    activity_date = parse_when(when)
    task_data: dict[str, Any] = {
        "WhatId": opp_id,
        "OwnerId": sf_user_id,
        "Subject": subject,
        "Description": notes,
        "ActivityDate": activity_date.isoformat(),
        "Status": "Completed",
        "Priority": "Normal",
    }

    # 5. Create Task.
    try:
        task_result = call_tool("sf_create_record", {"object_name": "Task", "data": task_data})
    except Exception as exc:
        log.exception("Failed to create Task on opp %s", opp_id)
        return {"status": "create_failed", "error": str(exc)}

    if not isinstance(task_result, dict) or not task_result.get("id"):
        err = (task_result.get("errors") or []) if isinstance(task_result, dict) else []
        return {"status": "create_failed", "error": str(err or task_result)}

    task_id: str = task_result["id"]

    # 6. Audit.
    try:
        audit.record_write(
            {
                "tool": "sf_log_touch",
                "slack_user": slack_user_id,
                "sf_user": sf_user_id,
                "opp_id": opp_id,
                "opp_name": opp_name,
                "task_id": task_id,
                "subject": subject,
            }
        )
    except Exception:
        log.exception("Audit write failed for task %s on opp %s (non-fatal)", task_id, opp_id)

    # 7. Return success.
    return {
        "status": "ok",
        "task_id": task_id,
        "opp_id": opp_id,
        "opp_url": _opp_url(opp_id),
        "subject": subject,
    }
