"""Research guest profiles using Gemini with Google Search grounding."""

import asyncio
import json
import os
import re

from google import genai
from google.genai import types

MODEL_ID = "gemini-2.5-flash"

PROFILE_PROMPT = """\
Research the following person and return a JSON object with these fields:
- "summary": a 2–4 sentence bio or description of who this person is
- "links": a list of relevant URLs (LinkedIn, company page, news articles, etc.)
- "photo_url": the most likely public photo URL if one can be found, otherwise null

Person: {full_name}
Location: {location}

Return ONLY the JSON object, no markdown fences or extra text.
If no public information is available, return:
{{"summary": "No public information found.", "links": [], "photo_url": null}}
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
        "links": data.get("links", []),
        "photo_url": data.get("photo_url"),
    }


async def _profile_one(client: genai.Client, guest: dict) -> dict:
    """Profile a single guest asynchronously."""
    full_name = f"{guest['first_name']} {guest['last_name']}".strip()
    location = _location_str(guest)
    prompt = PROFILE_PROMPT.format(full_name=full_name, location=location)

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    profile = _parse_profile(response.text or "")
    return {**guest, "profile": profile}


async def _profile_all(guests: list[dict]) -> list[dict]:
    client = _build_client()
    tasks = [_profile_one(client, g) for g in guests]
    return await asyncio.gather(*tasks)


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
