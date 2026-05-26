"""Wine-owner detection + public wine research.

Two best-effort capabilities, shared by the guest-report pipeline (async) and
the Slack sommelier bot (sync). Both degrade silently on failure so they can
never break the main flow.

1. ``find_winemaker(email)`` — ask Agent B (NetSuite, via the MCP server)
   whether an arriving guest is one of The Vines' wine owners, by matching their
   email against the customer table's brand-bearing records. Email-exact only:
   a fuzzy name match could falsely brand a guest a winemaker in a staff report,
   which is worse than missing one.

2. ``research_wine(brand, ...)`` — Gemini + Google Search grounding to pull
   PUBLIC info about a wine (style, tasting notes, blend, ratings, producer
   background), reusing the same grounded-search machinery as guest profiling.

The report pipeline links the two: a guest detected as a winemaker gets their
label researched on the open web and rendered in their card.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from google.genai import types

from gemini_profiler import GEMINI_MODEL, _build_gemini_client, _extract_grounding_urls
from mcp_client import call_tool

log = logging.getLogger(__name__)

# Lets the report cron disable the whole feature without code changes.
ENABLED = os.getenv("WINE_ENRICHMENT_ENABLED", "true").lower() not in ("0", "false", "no")

_GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "180"))

# Brand-field values that aren't a usable, searchable brand. Many owner records
# carry placeholders ("RA", "TBC", "pending approval", or a whole notes
# paragraph) rather than a finalized label — see the NetSuite customer data.
_PLACEHOLDER_BRANDS = frozenset(
    {"none", "various", "varios", "tbc", "tba", "n/a", "na", "pending", "varias"}
)
_PLACEHOLDER_MARKERS = (
    "pending approval", "no usar", "don't have", "dont have", "sin marca",
    "aprobada", "(pending", "to be confirmed",
)


# ── Winemaker detection (NetSuite via Agent B) ─────────────────────────────

def _clean_email(email: str | None) -> str | None:
    """Normalize an email for an exact SuiteQL match, or None if unusable.

    Rejects anything with a quote or whitespace so it can be safely inlined in
    the query (SuiteQL has no parameter binding through this path).
    """
    if not email:
        return None
    e = email.strip().lower()
    if "@" not in e or "'" in e or '"' in e or any(c.isspace() for c in e):
        return None
    return e


def _winemaker_query(email: str) -> str:
    """SuiteQL: brand-bearing customer whose primary OR secondary email matches."""
    return (
        "SELECT id, entityid, companyname, "
        "custentity_vom_winebrandname AS brand, "
        "custentity_vom_ownercode AS owner_code "
        "FROM customer "
        "WHERE custentity_vom_winebrandname IS NOT NULL "
        f"AND (LOWER(email) = '{email}' "
        f"OR LOWER(custentity_secondaryemail) = '{email}')"
    )


def _rows_from_result(res) -> list[dict]:
    """Coerce an MCP ns_suiteql_query result into a list of row dicts.

    The tool may return a bare list, a {'result': [...]} / {'items': [...]}
    wrapper, or an {'error': ...} dict; treat anything else as no rows.
    """
    if isinstance(res, list):
        rows = res
    elif isinstance(res, dict):
        if "error" in res:
            log.warning("Winemaker NetSuite query returned an error: %s", res["error"])
            return []
        rows = res.get("result") or res.get("items") or []
    else:
        return []
    return [r for r in rows if isinstance(r, dict) and "error" not in r]


def find_winemaker(email: str | None) -> dict | None:
    """Return the guest's wine-owner record from NetSuite, or None.

    Synchronous (uses the requests-based MCP client). Best-effort: any failure
    is logged and swallowed, returning None.
    """
    if not ENABLED:
        return None
    clean = _clean_email(email)
    if not clean:
        return None
    try:
        res = call_tool("ns_suiteql_query", {"query": _winemaker_query(clean)})
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the report
        log.warning("Winemaker lookup failed for %s: %s", email, exc)
        return None

    rows = _rows_from_result(res)
    if not rows:
        return None
    row = rows[0]
    return {
        "customer_id": row.get("id"),
        "customer_name": row.get("companyname") or row.get("entityid"),
        "brand": (row.get("brand") or "").strip(),
        "owner_code": row.get("owner_code"),
    }


# ── Public wine research (Gemini + Google Search grounding) ────────────────

def _is_researchable_brand(brand: str | None) -> bool:
    """True if the brand looks like a real, searchable label (not a placeholder)."""
    if not brand:
        return False
    norm = brand.strip().lower()
    if len(norm) < 3 or norm in _PLACEHOLDER_BRANDS:
        return False
    if any(marker in norm for marker in _PLACEHOLDER_MARKERS):
        return False
    # A long free-text value is an internal note, not a brand name.
    if len(norm.split()) > 8:
        return False
    return True


WINE_PROMPT = """\
You are helping sommeliers and staff at The Vines of Mendoza, a luxury wine
hotel in the Uco Valley, Mendoza, Argentina. Private clients make small-production
wines under their own labels at the on-site winery, so many of these brands have
LITTLE OR NO public web presence. Do not invent information.

Research the wine below and return a JSON object with these fields:
- "found": true only if you found public information that is plausibly about THIS
  specific wine/producer; false if you found nothing credible.
- "summary": 2–4 sentences for staff describing the wine and producer. If nothing
  was found, say so plainly.
- "style": short phrase, e.g. "Malbec-led Bordeaux blend" (empty string if unknown).
- "blend": grape breakdown if known, e.g. "64% Malbec, 24% Merlot, 12% Cabernet
  Franc" (empty string if unknown).
- "tasting_notes": aroma/palate notes if available (empty string if unknown).
- "producer_background": background on the producer/vineyard/label name origin
  (empty string if unknown).
- "food_pairing": suggested pairings if available (empty string if unknown).
- "ratings": a list of objects {{"source": "Vivino", "score": "4.3", "count": 274}}
  for any critic/aggregator scores you find (Vivino, Wine Enthusiast, Wine
  Spectator, etc.). Use [] if none. "count" may be null.
- "confidence": integer 0–10 for how sure you are this is the right wine. Score
  LOW when the brand is generic or you could not distinguish it from other wines.

Wine brand/label: {brand}
{context}
Return ONLY the JSON object, no markdown fences or extra text.
If you find nothing credible, return:
{{"found": false, "summary": "No public information found for this wine.", "style": "", "blend": "", "tasting_notes": "", "producer_background": "", "food_pairing": "", "ratings": [], "confidence": 0}}
"""


def _parse_wine_research(text: str) -> dict:
    """Extract the wine-research JSON, stripping markdown fences. Raises on bad JSON."""
    text = re.sub(r"^```(?:json)?\s*", "", (text or "").strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    data = json.loads(text)
    ratings = data.get("ratings") or []
    if not isinstance(ratings, list):
        ratings = []
    return {
        "found": bool(data.get("found", False)),
        "summary": (data.get("summary") or "").strip(),
        "style": (data.get("style") or "").strip(),
        "blend": (data.get("blend") or "").strip(),
        "tasting_notes": (data.get("tasting_notes") or "").strip(),
        "producer_background": (data.get("producer_background") or "").strip(),
        "food_pairing": (data.get("food_pairing") or "").strip(),
        "ratings": [r for r in ratings if isinstance(r, dict)],
        "confidence": int(data.get("confidence", 0) or 0),
    }


def _wine_prompt(brand: str, owner_name: str | None, vintage: str | None) -> str:
    context_parts = []
    if owner_name:
        context_parts.append(
            f"The label belongs to {owner_name}, a private wine-owner client of "
            "The Vines of Mendoza."
        )
    if vintage:
        context_parts.append(f"Vintage of interest: {vintage}.")
    context = "\n".join(context_parts)
    return WINE_PROMPT.format(brand=brand, context=context)


def research_wine_sync(
    brand: str,
    owner_name: str | None = None,
    vintage: str | None = None,
    gemini=None,
) -> dict | None:
    """Research public web info about a wine. Synchronous. Best-effort → None on failure.

    Pass an existing Gemini client via ``gemini`` to reuse it; otherwise one is
    built for this call.
    """
    if not brand or not brand.strip():
        return None
    client = gemini or _build_gemini_client()
    prompt = _wine_prompt(brand.strip(), owner_name, vintage)
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
            ),
        )
        result = _parse_wine_research(response.text or "")
    except json.JSONDecodeError as exc:
        log.warning("Wine research JSON parse failed for %r: %s", brand, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Wine research failed for %r: %s", brand, exc)
        return None

    urls, queries = _extract_grounding_urls(response)
    if queries:
        log.info("  Wine research searched: %s", " | ".join(queries))
    result["sources"] = urls[:6]
    return result


async def research_wine(
    brand: str,
    owner_name: str | None = None,
    vintage: str | None = None,
    gemini=None,
) -> dict | None:
    """Async wrapper around :func:`research_wine_sync` for the report pipeline."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(research_wine_sync, brand, owner_name, vintage, gemini),
            timeout=_GEMINI_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning("Wine research timed out for %r.", brand)
        return None


async def attach_wine_owner(profiled: dict, gemini=None) -> dict:
    """Best-effort: tag a profiled guest as a winemaker and research their label.

    Mutates and returns ``profiled``. Sets ``profiled['winemaker']`` to a dict
    (with an optional ``research`` sub-dict) when the guest matches a NetSuite
    wine-owner record; leaves ``profiled`` untouched on any failure.
    """
    if not ENABLED:
        return profiled
    email = profiled.get("email")
    full_name = f"{profiled.get('first_name', '')} {profiled.get('last_name', '')}".strip()
    try:
        owner = await asyncio.to_thread(find_winemaker, email)
    except Exception as exc:  # noqa: BLE001
        log.warning("Winemaker enrichment errored for %s: %s", full_name or email, exc)
        return profiled
    if not owner:
        return profiled

    log.info("  %s is a wine owner (brand=%r, code=%s).",
             full_name or email, owner.get("brand"), owner.get("owner_code"))
    winemaker = dict(owner)
    brand = owner.get("brand") or ""
    if _is_researchable_brand(brand):
        research = await research_wine(brand, owner_name=full_name or None, gemini=gemini)
        if research:
            winemaker["research"] = research
    profiled["winemaker"] = winemaker
    return profiled
