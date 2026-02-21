"""Research guest profiles using Gemini with Google Search grounding."""

import asyncio
import json
import logging
import os
import re

import requests
from google import genai
from google.genai import types

log = logging.getLogger(__name__)

MODEL_ID = "gemini-2.5-flash"

PROFILE_PROMPT = """\
You are helping staff at The Vines of Mendoza, a luxury private residence and wine hotel in
Mendoza, Argentina, prepare for arriving guests. Guests are typically affluent international
travelers interested in wine, food, and high-end tourism.

Research the person below and return a JSON object. Every field must refer to the SAME
individual. Use the guest's home city/country and email domain (if provided) to pick the most
likely match when multiple people share the name.

Fields:
- "summary": a 2–4 sentence bio in English
- "summary_es": the same bio translated into Spanish
- "links": URLs for this specific person only. Include all of the following that you can find
  and confirm belong to this exact person:
    • LinkedIn profile
    • Instagram profile
    • Twitter/X profile
    • Facebook profile
    • Any other social media (YouTube, TikTok, Threads, etc.)
    • Official company/personal website or bio page
    • Major news or publication articles
  Do NOT mix links from different people with the same name.
  Do NOT include people-search aggregators (Spokeo, Whitepages, BeenVerified, etc.).
  Only include a URL if you are confident it is active and belongs to this exact person.
- "photo_url": a direct image URL (.jpg/.jpeg/.png/.webp) of a headshot for this exact person.
  Search broadly: visit bio or team pages on their employer's website, their personal website,
  Twitter/X profile (pbs.twimg.com images load without authentication), author pages on
  publications or magazines where their work appears, press releases, and news articles.
  Extract the direct image src URL from the page HTML — do not guess at URL patterns.
  Do NOT use LinkedIn or Instagram — their CDN images require authentication and will not load.
  Return null if you are not confident it is the correct person or the image requires authentication.
- "confidence": an integer 0–10. Apply these rules strictly:
    0–2: Almost no verifiable public information, or the name is extremely common with no way
         to identify the specific individual.
    3–4: Common name with several people sharing it; selected best guess based on location but
         uncertain. Or very little public info found.
    5–6: Moderate confidence — one plausible match found that fits the location/context, but
         other people with this name exist and could not be fully ruled out.
    7–8: Good confidence — strong match, limited name ambiguity, profile fits the context of
         a guest at a luxury wine destination.
    9–10: Very high confidence — clear public figure or unique name with unambiguous, well-sourced
          information. Use 10 only for globally recognized individuals.
  When in doubt, score LOWER. It is better to flag uncertainty than to present wrong information
  as fact. A name like "Bryan Driscoll" from the USA with many LinkedIn results should score ≤ 4.
- "confidence_reason": one concise sentence explaining the score

Person: {full_name}
Home location: {location}
Email domain: {email_domain}

Return ONLY the JSON object, no markdown fences or extra text.
If no public information is available, return:
{{"summary": "No public information found.", "summary_es": "No se encontró información pública.", "links": [], "photo_url": null, "confidence": 0, "confidence_reason": "No public information found."}}
"""

PHOTO_PROMPT = """\
Find a publicly accessible profile photo or headshot for the person below.

Search broadly: visit bio or team pages on their employer's website, their personal website,
Twitter/X profile (pbs.twimg.com/profile_images/... URLs are publicly accessible without
authentication), author pages on publications or magazines where their work appears, press
releases, and news articles. Visit the actual pages and extract the direct image src URL from
the HTML — do not guess at URL patterns.

Do NOT use LinkedIn or Instagram — their CDN images require authentication and will not load in
email clients.

Return ONLY a JSON object with two fields:
- "photo_url": a direct image URL ending in .jpg, .jpeg, .png, or .webp that does not require
  authentication to load, or null if nothing reliable was found.
- "source_url": the page URL where the photo was found, or null if no photo was found.

Person: {full_name}
Home location: {location}
Additional hint: {hint_url}

Return ONLY the JSON object, no markdown fences or extra text.
"""


def _build_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def _location_str(guest: dict) -> str:
    parts = [guest.get("city", ""), guest.get("state", ""), guest.get("country", "")]
    return ", ".join(p for p in parts if p)


def _parse_profile(text: str) -> dict:
    """Extract JSON from the model response, stripping any markdown fences."""
    # Strip ```json ... ``` fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"summary": "Could not parse profile.", "links": [], "photo_url": None}
    return {
        "summary": data.get("summary", ""),
        "summary_es": data.get("summary_es", ""),
        "links": data.get("links", []),
        "photo_url": data.get("photo_url"),
        "confidence": int(data.get("confidence", 0)),
        "confidence_reason": data.get("confidence_reason", ""),
    }


def _check_link(url: str) -> bool:
    """Return True if the URL responds with a success status."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True, headers=headers)
        if resp.status_code < 400:
            return True
        # Some servers (especially image CDNs) reject HEAD — fall back to GET
        resp = requests.get(url, timeout=5, allow_redirects=True, headers=headers, stream=True)
        return resp.status_code < 400
    except Exception:
        return False


async def _validate_links(links: list[str]) -> list[str]:
    """Filter links down to only those that are reachable."""
    if not links:
        return []
    log.info("    Validating %d link(s)…", len(links))
    results = await asyncio.gather(*[asyncio.to_thread(_check_link, url) for url in links])
    valid = [url for url, ok in zip(links, results) if ok]
    dead = len(links) - len(valid)
    if dead:
        log.info("    %d link(s) kept, %d dead link(s) removed.", len(valid), dead)
    else:
        log.info("    All %d link(s) are reachable.", len(valid))
    return valid


async def _find_photo(client: genai.Client, guest: dict, hint_url: str) -> tuple[str | None, str | None]:
    """Search for a publicly accessible profile photo. Returns (photo_url, source_url)."""
    full_name = f"{guest['first_name']} {guest['last_name']}".strip()
    location = _location_str(guest)
    log.info("    Searching for photo via fallback (Twitter/X, company pages, news)…")
    prompt = PHOTO_PROMPT.format(full_name=full_name, location=location, hint_url=hint_url or "none")
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        text = re.sub(r"^```(?:json)?\s*", "", (response.text or "").strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())
        data = json.loads(text)
        photo_url = data.get("photo_url") or None
        source_url = data.get("source_url") or None
        if photo_url and await asyncio.to_thread(_check_link, photo_url):
            log.info("    Fallback photo found: %s", photo_url)
            return photo_url, source_url
        log.info("    No usable fallback photo found.")
        return None, None
    except Exception:
        log.warning("    Photo fallback search failed.")
        return None, None


async def _profile_one(client: genai.Client, guest: dict) -> dict:
    """Profile a single guest asynchronously."""
    full_name = f"{guest['first_name']} {guest['last_name']}".strip()
    location = _location_str(guest)
    email = guest.get("email", "")
    email_domain = email.split("@")[-1] if "@" in email else "unknown"
    log.info("Profiling: %s (%s)…", full_name, location or "location unknown")
    prompt = PROFILE_PROMPT.format(full_name=full_name, location=location, email_domain=email_domain)

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    profile = _parse_profile(response.text or "")
    log.info(
        "  Profile received — confidence: %d/10 | links: %d | photo: %s",
        profile["confidence"],
        len(profile["links"]),
        "yes" if profile["photo_url"] else "none",
    )

    profile["links"] = await _validate_links(profile["links"])

    # Validate photo URL; clear it if dead
    if profile["photo_url"] and not await asyncio.to_thread(_check_link, profile["photo_url"]):
        log.info("    Primary photo URL is dead, clearing.")
        profile["photo_url"] = None

    # If still no photo, search Twitter/X, company pages, etc.
    if not profile["photo_url"]:
        hint = profile["links"][0] if profile["links"] else ""
        photo_url, source_url = await _find_photo(client, guest, hint)
        profile["photo_url"] = photo_url
        # Add the source page to links if it's new
        if source_url and source_url not in profile["links"]:
            if await asyncio.to_thread(_check_link, source_url):
                profile["links"].append(source_url)

    log.info("  Done: %s — confidence: %d/10 | links: %d | photo: %s",
        full_name, profile["confidence"], len(profile["links"]),
        "found" if profile["photo_url"] else "not found",
    )
    return {**guest, "profile": profile}


async def _profile_all(guests: list[dict]) -> list[dict]:
    client = _build_client()
    # Stagger requests to stay within the API rate limit.
    # Default is 60/min (1/sec); raise GEMINI_RPM if on a higher quota tier.
    rpm = int(os.environ.get("GEMINI_RPM", "60"))
    delay = 60.0 / rpm  # seconds between request starts
    log.info("Profiling %d guest(s) with %s at %d RPM (%.1fs stagger)…", len(guests), MODEL_ID, rpm, delay)

    async def staggered(i: int, guest: dict) -> dict:
        await asyncio.sleep(i * delay)
        return await _profile_one(client, guest)

    tasks = [staggered(i, g) for i, g in enumerate(guests)]
    results = await asyncio.gather(*tasks)
    log.info("All guests profiled.")
    return results


def profile_guests(guests: list[dict]) -> list[dict]:
    """Return guests list with an added 'profile' key for each entry."""
    if not guests:
        return []
    return asyncio.run(_profile_all(guests))


# Convenience wrapper for isolated testing
def profile_guest(guest: dict) -> dict:
    """Profile a single guest dict synchronously."""
    results = profile_guests([guest])
    return results[0] if results else {**guest, "profile": {"summary": "", "links": [], "photo_url": None}}
