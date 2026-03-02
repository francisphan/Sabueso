"""Research guest profiles using Gemini (research) + Claude (vision checks)."""

import asyncio
import base64
import contextvars
import json
import logging
import os
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import anthropic
import requests
from google import genai
from google.genai import types

log = logging.getLogger(__name__)

# Contextvar holds the current guest name for log tagging during concurrent profiling.
# Read by _GuestTagFilter (installed in scheduler.py) to prefix log lines.
current_guest: contextvars.ContextVar[str] = contextvars.ContextVar("current_guest", default="")

GEMINI_MODEL = "gemini-2.5-flash"   # web research & photo search
CLAUDE_MODEL = "claude-sonnet-4-6"  # vision checks & consistency selection

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
    • Official company/personal website or bio page — if the email domain is provided, check
      that domain's /about, /team, /people, or /staff page for a bio or profile of this person
    • The person's OWN website — many professionals (writers, critics, consultants, executives)
      operate a personal site under their name (e.g. firstnamelastname.com). Search specifically
      for "site:{{first}}{{last}}.com" or "{{full name}} official website" and visit the result if found.
      Always include this URL in links if it exists.
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
  as fact. A common name with many unrelated results (e.g. a generic American name with dozens
  of LinkedIn profiles) should score ≤ 4 unless context clearly identifies the right individual.
- "confidence_reason": one concise sentence explaining the score

Person: {full_name}
Home location: {location}
Email domain: {email_domain}
{existing_context}
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


def _build_gemini_client() -> genai.Client:
    # http_options timeout (seconds) sets the underlying httpx socket read timeout so that
    # Gemini API calls stalled at the TCP level will eventually fail rather than hanging
    # the thread pool when asyncio.run() calls shutdown_default_executor() on cleanup.
    _GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "180"))
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options={"timeout": _GEMINI_TIMEOUT * 1000},  # SDK expects milliseconds
    )


def _build_claude_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _location_str(guest: dict) -> str:
    parts = [guest.get("city", ""), guest.get("state", ""), guest.get("country", "")]
    return ", ".join(p for p in parts if p)


def _parse_profile(text: str) -> dict:
    """Extract JSON from the model response, stripping any markdown fences.

    Raises json.JSONDecodeError if the response cannot be parsed, so callers
    can distinguish a parse failure from an API failure and retry if needed.
    """
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    data = json.loads(text)  # intentionally raises on bad JSON
    return {
        "summary": data.get("summary", ""),
        "summary_es": data.get("summary_es", ""),
        "links": data.get("links", []),
        "photo_url": data.get("photo_url"),
        "confidence": int(data.get("confidence", 0)),
        "confidence_reason": data.get("confidence_reason", ""),
    }


def _extract_grounding_urls(response) -> tuple[list[str], list[str]]:
    """Extract verified source URLs and search queries from Gemini's groundingMetadata.

    Returns (urls, queries). These are backend-generated facts — Gemini visited these
    pages to build the response — so they are far more reliable than URLs in the text.
    """
    try:
        meta = response.candidates[0].grounding_metadata
        chunks = meta.grounding_chunks or []
        urls = [c.web.uri for c in chunks if c.web and c.web.uri]
        queries = list(meta.web_search_queries or [])
        return urls, queries
    except (AttributeError, IndexError):
        return [], []


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


def _resolve_redirect(url: str) -> str:
    """Follow HTTP redirects and return the final URL. Returns original on failure.

    Used to unwrap Google's grounding-api-redirect wrapper URLs before storing
    them in the report so readers see the actual destination, not the proxy URL.
    """
    try:
        resp = requests.head(url, timeout=(4, 8), allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        return resp.url
    except Exception:
        return url


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


_PROFILE_URL_SIGNALS = re.compile(
    r'(profile|headshot|portrait|bio|author|staff|team|about|people|person|speaker)',
    re.IGNORECASE,
)
_NOISE_URL_SIGNALS = re.compile(
    r'(banner|hero|background|logo|icon|thumbnail|cover|placeholder|avatar-default'
    r'|header|featured|splash|wide|social-share|og[-_]image)',
    re.IGNORECASE,
)
_BIO_PAGE_SIGNALS = re.compile(
    r'/(about|bio|author|team|people|person|staff|speaker|profile)(/|$|-|\?)',
    re.IGNORECASE,
)
# Matches landscape dimension patterns in URLs like 1200x628, 1280_720, 800-400
_WIDE_DIMENSION_RE = re.compile(r'(\d{3,4})[x_\-](\d{3,4})')


def _score_photo_candidate(
    img_url: str, name_parts: list[str], is_meta_tag: bool, source_page_url: str = ""
) -> int:
    """Return a relevance score for a candidate image URL. Higher is better."""
    score = 0
    lower = img_url.lower()

    # Strong signal: person's name appears in the image URL
    if any(p in lower for p in name_parts):
        score += 10

    # Medium signal: URL path looks like a profile/bio/author image.
    # Requires either the person's name to also appear in the URL, or the source page
    # to be a bio page — prevents article author headshots (e.g. "Email-Edit-Headshot_Lindsay.png"
    # on a publication article about the guest's employer) from scoring as strong candidates.
    # NOTE: check only the URL *path* (not the domain) for name presence — avoids false
    # positives like "atkin" matching in the domain "timatkin.com" on an unrelated page.
    source_path = urlparse(source_page_url).path.lower() if source_page_url else ""
    if _PROFILE_URL_SIGNALS.search(lower):
        on_bio_source = bool(
            _BIO_PAGE_SIGNALS.search(source_page_url)
            or any(p in source_path for p in name_parts)
        )
        name_in_img_url = any(p in lower for p in name_parts)
        if name_in_img_url or on_bio_source:
            score += 3

    # og:image is only trustworthy when scraping a bio/profile page;
    # on article pages it typically shows the article's featured image, not a headshot
    if is_meta_tag:
        on_bio_page = bool(
            _BIO_PAGE_SIGNALS.search(source_page_url)
            or any(p in source_path for p in name_parts)
        )
        score += 3 if on_bio_page else 0

    # Known profile image CDN (Twitter/X)
    if "pbs.twimg.com/profile_images" in lower:
        score += 4

    # Penalty: URL looks like a banner, logo, or generic site asset.
    # Kept at -2 (not -5) so that when strong confirming signals are present —
    # e.g. the person's name appears in the HTML context around the image on their
    # own bio page — the image can still reach the vision-check threshold. Vision
    # is the real safety net for images that pass despite this signal.
    if _NOISE_URL_SIGNALS.search(lower):
        score -= 2

    # Penalty: generic numbered photo filename (e.g. team06.jpg, staff3.png, member2.jpg)
    # that does NOT contain the person's name — likely a generic slot in a team gallery.
    if (not any(p in lower for p in name_parts)
            and re.search(r'(?:team|staff|member|speaker|person)\d+\.', lower)):
        score -= 4

    # Penalty: filename looks like an article title (5+ hyphen-separated words, no person's name)
    # e.g. "i-will-lose-it-if-one-more-person-tells-me.png" — an article slug, not a headshot.
    # Person-named files like "annie-daly-headshot.jpg" are unaffected because name_parts match.
    # UUID/hash filenames (e.g. 8c4f3a81-7d2d-40cc-8446-1a3cf4048674_thumb) are excluded —
    # they are CDN content IDs whose segments contain hex digits, not natural-language words.
    filename_stem = lower.rsplit("/", 1)[-1].split("?")[0].rsplit(".", 1)[0]
    filename_words = re.split(r"[-_]", filename_stem)
    if (len(filename_words) >= 5
            and not any(p in filename_stem for p in name_parts)
            and not any(re.search(r"[0-9]", w) for w in filename_words)):
        score -= 3

    # Penalty: landscape dimensions in the URL suggest a banner/header crop.
    # Threshold 1.5 catches 3:2 landscape images (e.g. 1320x866 = 1.52) that are
    # clearly not portrait headshots, while preserving slightly wide but portrait-
    # oriented crops (e.g. 4:3 at 1.33 is fine).
    for m in _WIDE_DIMENSION_RE.finditer(lower):
        w, h = int(m.group(1)), int(m.group(2))
        if w > 100 and h > 100 and w / h > 1.5:
            score -= 4
            break

    return score


def _extract_jsonld_person_images(text: str) -> list[str]:
    """Extract image URLs from schema.org/Person JSON-LD blocks embedded in a page."""
    images = []
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(block)
            items = data if isinstance(data, list) else [data]
            # Flatten @graph arrays
            expanded = []
            for item in items:
                if isinstance(item, dict) and "@graph" in item:
                    expanded.extend(item["@graph"])
                else:
                    expanded.append(item)
            for item in expanded:
                if not isinstance(item, dict):
                    continue
                schema_type = item.get("@type", "")
                types_list = schema_type if isinstance(schema_type, list) else [schema_type]
                if "Person" not in types_list:
                    continue
                img = item.get("image") or item.get("photo")
                if isinstance(img, str) and img.startswith("http"):
                    images.append(img)
                elif isinstance(img, dict):
                    url = img.get("url") or img.get("contentUrl", "")
                    if url.startswith("http"):
                        images.append(url)
        except (json.JSONDecodeError, AttributeError):
            continue
    return images


_BIO_LINK_PATH = re.compile(
    r'/(about|bio|author|team|people|person|staff|speaker|profile|leadership|management'
    r'|bios|who-we-are|meet(?:-the)?-team|our-team)(/|$|-|\?)',
    re.IGNORECASE,
)


def _extract_bio_links(base_url: str, html: str, name_parts: list[str]) -> list[str]:
    """Return internal links from the page that look like bio/team subpages.

    Prioritises links whose path matches known bio-page patterns or contains
    the person's name slug (e.g. /team/jason-schechter). Limited to 10 results
    so the second-pass scrape stays bounded.
    """
    base_netloc = urlparse(base_url).netloc
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for href in re.findall(r'<a[^>]+href=["\']([^"\'#?][^"\']*)["\']', html, re.IGNORECASE):
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc != base_netloc or abs_url in seen:
            continue
        seen.add(abs_url)
        path = parsed.path.lower()
        link_score = 0
        if _BIO_LINK_PATH.search(path):
            link_score += 3
        if any(p in path for p in name_parts):
            link_score += 5  # name slug in URL — very likely their specific bio page
        if link_score > 0:
            scored.append((link_score, abs_url))
    scored.sort(reverse=True)
    return [u for _, u in scored[:10]]


def _extract_social_card_photo(url: str) -> str | None:
    """Extract a real photo URL embedded inside a social-card generator URL.

    Platforms like theorg.com produce og:image URLs that render a composite card
    (person photo + job title + company logo overlaid as a PNG). The actual CDN
    photo is URL-encoded in an 'image' (or similar) query parameter. Extracting
    it lets us score and vision-check the real headshot instead of the card.

    Returns None if no embedded image URL is found.
    """
    # og:image URLs extracted from raw HTML contain HTML entities (&amp; instead of &).
    # parse_qs splits on literal '&', so we must unescape first.
    url = url.replace("&amp;", "&")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("image", "img", "photo", "avatar", "picture"):
        for val in params.get(key, []):
            decoded = unquote(val)
            if decoded.startswith("http") and re.search(r'\.(jpe?g|png|webp)(\?|$)', decoded, re.IGNORECASE):
                return decoded
    return None


def _scrape_photo_candidates_from_page(
    url: str, name_parts: list[str]
) -> tuple[list[tuple[int, str, str]], list[str]]:
    """Fetch a page and return (image_candidates, discovered_bio_links).

    image_candidates: list of (score, img_url, source_url) tuples
    discovered_bio_links: internal links that look like bio/team subpages,
                          suitable for a second-pass scrape
    """
    candidates: list[tuple[int, str, str]] = []
    bio_links: list[str] = []
    try:
        resp = requests.get(url, timeout=(4, 8), allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return candidates, bio_links
        # Use the final destination URL (after redirects) for scoring — redirect wrapper
        # URLs like vertexaisearch.cloud.google.com/grounding-api-redirect/... won't match
        # bio-page path patterns, so photos on the actual destination page would score too low.
        effective_url = resp.url
        text = resp.text[:300_000]
        text_lower = text.lower()

        # Page-level name presence: does this page actually mention the person?
        # +2 if yes (at least relevant), -6 if no (team page with many unrelated headshots)
        name_on_page = bool(name_parts) and all(p in text_lower for p in name_parts)
        page_bonus = 2 if name_on_page else -6

        # Discover internal bio/team links for second-pass scraping
        bio_links = _extract_bio_links(effective_url, text, name_parts)

        # schema.org/Person JSON-LD — highest-signal source: explicitly tagged as this person.
        # Bypass page_bonus — JSON-LD Person is trusted regardless.
        for img_url in _extract_jsonld_person_images(text):
            if re.search(r'\.(jpe?g|png|webp)(\?|$)', img_url, re.IGNORECASE):
                score = _score_photo_candidate(img_url, name_parts, is_meta_tag=False, source_page_url=effective_url)
                score += 7
                candidates.append((score, img_url, effective_url))

        # Extract full <img> tags so we can check alt/title and surrounding HTML context
        for img_tag in re.finditer(r'<img[^>]+>', text, re.IGNORECASE):
            tag_html = img_tag.group(0)

            url_m = re.search(r'(?:src|data-src)=["\'](https?://[^"\']+)["\']', tag_html, re.IGNORECASE)
            if not url_m:
                continue
            img_url = url_m.group(1)
            if not re.search(r'\.(jpe?g|png|webp)(\?|$)', img_url, re.IGNORECASE):
                continue

            score = _score_photo_candidate(img_url, name_parts, is_meta_tag=False, source_page_url=effective_url)
            score += page_bonus

            # Alt or title attribute contains the person's name — strong identity signal
            attr_m = re.search(r'(?:alt|title)=["\'](.*?)["\']', tag_html, re.IGNORECASE)
            if attr_m and any(p in attr_m.group(1).lower() for p in name_parts):
                score += 8

            # Person's name appears in the HTML surrounding this image (within 300 chars each side)
            start, end = img_tag.start(), img_tag.end()
            context = text_lower[max(0, start - 300): end + 300]
            if all(p in context for p in name_parts):
                score += 5  # both first and last name visible near this image
            elif any(p in context for p in name_parts):
                score += 2  # at least one name part nearby

            candidates.append((score, img_url, effective_url))

        # og:image / twitter:image meta tags
        # Note: [^"\'>\s]+ allows query parameters (e.g. ?w=600&h=600) in the URL.
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\'>\s]+)',
            r'<meta[^>]+content=["\'](https?://[^"\'>\s]+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\'](https?://[^"\'>\s]+)',
            r'<meta[^>]+content=["\'](https?://[^"\'>\s]+)["\'][^>]+name=["\']twitter:image["\']',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                img_url = m.group(1)
                # Some platforms (e.g. theorg.com) produce social-card OG images that
                # are a rendered composite (photo + title/company text overlaid).
                # The actual headshot CDN URL is embedded as an 'image' query param.
                # Extract it so we score and download the real photo, not the card.
                extracted = _extract_social_card_photo(img_url)
                if extracted:
                    log.debug("  Social-card OG image: extracted real photo %s", extracted)
                    img_url = extracted
                # Accept URLs with image extension anywhere (before or within query params)
                if re.search(r'\.(jpe?g|png|webp)', img_url, re.IGNORECASE):
                    score = _score_photo_candidate(img_url, name_parts, is_meta_tag=True, source_page_url=effective_url)
                    score += page_bonus
                    candidates.append((score, img_url, effective_url))

    except Exception:
        pass
    return candidates, bio_links


async def _scrape_photo_candidates_from_links(
    links: list[str], full_name: str, scrape_sem: asyncio.Semaphore
) -> list[tuple[int, str, str]]:
    """Scrape all known profile pages and return the full scored candidate list.

    scrape_sem caps total concurrent HTTP page fetches across all guests to
    prevent thread pool exhaustion. After the first pass, follows internal
    bio/team links discovered on those pages (one level deep).
    """
    name_parts = [p.lower() for p in full_name.split() if len(p) > 2]

    async def fetch(url: str) -> tuple[list[tuple[int, str, str]], list[str]]:
        async with scrape_sem:
            return await asyncio.to_thread(_scrape_photo_candidates_from_page, url, name_parts)

    # First pass
    first_results = await asyncio.gather(*[fetch(url) for url in links])
    all_candidates = [c for candidates, _ in first_results for c in candidates]

    # Collect discovered bio links, skip any already visited
    visited = set(links)
    discovered: list[str] = []
    for _, bio_links in first_results:
        for link in bio_links:
            if link not in visited:
                discovered.append(link)
                visited.add(link)

    # Second pass — follow internal bio/team links (cap at 6 per guest)
    if discovered:
        log.info("  Following %d discovered bio link(s) for %s…", len(discovered[:6]), full_name)
        second_results = await asyncio.gather(*[fetch(url) for url in discovered[:6]])
        all_candidates.extend(c for candidates, _ in second_results for c in candidates)

    return all_candidates


def _is_bio_grounding_url(url: str, name_parts: list[str], email_domain: str = "") -> bool:
    """Return True if a Gemini grounding URL is worth scraping for photos.

    Filters out generic article pages (which contain author headshots unrelated
    to the guest) while keeping bio/profile/team pages and employer-domain pages.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    # Always scrape pages from the guest's own employer domain
    if email_domain and email_domain not in ("unknown", "") and email_domain in netloc:
        return True
    # Bio/profile/team/author URL path patterns
    if _BIO_LINK_PATH.search(parsed.path.lower()):
        return True
    # Person's name slug present in the URL path (e.g. /jason-schechter, /annie-daly-bio)
    slug = parsed.path.lower().replace("-", " ").replace("_", " ").replace("/", " ")
    if sum(1 for p in name_parts if p in slug) >= 2:
        return True
    return False


def _email_domain_bio_pages(email_domain: str, full_name: str = "") -> list[str]:
    """Return candidate bio/team pages derived directly from the guest's email domain.

    These are scraped unconditionally — no search required — so Bryan Driscoll's
    employer page is always checked regardless of what Gemini returns in links.

    Also generates name-slug URL variants (e.g. /team-member/jason-schechter) so
    that sites using per-person bio URLs are found even when the team-list page
    is dynamically rendered and we can't crawl individual links from it.
    """
    if not email_domain or email_domain == "unknown" or "." not in email_domain:
        return []
    base = f"https://{email_domain}"
    pages = [
        f"{base}/about",
        f"{base}/about-us",
        f"{base}/team",
        f"{base}/our-team",
        f"{base}/people",
        f"{base}/staff",
        f"{base}/who-we-are",
        f"{base}/leadership",
    ]
    # Also probe name-slug URL patterns for sites that publish individual bio pages
    # (e.g. hildenecap.com/team-member/jason-schechter).
    if full_name:
        name_parts = [p.lower() for p in full_name.split() if len(p) > 1]
        if len(name_parts) >= 2:
            first, last = name_parts[0], name_parts[-1]
            slug_fl = f"{first}-{last}"  # first-last
            slug_lf = f"{last}-{first}"  # last-first
            for prefix in ("/team-member", "/team", "/people", "/person",
                           "/staff", "/about/team", "/leadership"):
                pages.append(f"{base}{prefix}/{slug_fl}")
                pages.append(f"{base}{prefix}/{slug_lf}")
    return pages


def _name_based_domains(full_name: str) -> list[str]:
    """Return candidate personal website URLs derived from the guest's name.

    Many professionals (writers, critics, consultants) run a personal site
    under their name — e.g. timatkin.com, bryandriscoll.com.  We probe the
    most common patterns so the scraping step always checks these even when
    Gemini's grounding search doesn't visit them.
    """
    parts = [p.lower() for p in full_name.split() if len(p) > 1]
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]
    domains = [
        f"{first}{last}.com",       # timatkin.com
        f"{first}-{last}.com",      # tim-atkin.com
        f"{first}{last}.co.uk",     # timatkin.co.uk
        f"{last}{first}.com",       # atkintim.com (rare but worth checking)
    ]
    pages: list[str] = []
    for domain in domains:
        try:
            resp = requests.head(
                f"https://{domain}", timeout=(3, 5), allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code < 400:
                base = f"https://{domain}"
                pages.append(base)
                pages.append(f"{base}/about")
                pages.append(f"{base}/biography")
                pages.append(f"{base}/bio")
                log.info("  Name-based domain found: %s (status %d)", domain, resp.status_code)
        except Exception:
            pass
    return pages



async def _check_is_headshot(
    client: anthropic.AsyncAnthropic,
    sem: asyncio.Semaphore,
    img_url: str,
    full_name: str = "",
) -> tuple[bool, str | None, str | None]:
    """Download img_url and ask Claude Vision whether it is a headshot of a single person.

    Returns (is_headshot, description, face_position) where:
    - description is a one-sentence physical description for cross-referencing
    - face_position is a CSS object-position value (e.g. "50% 25%") locating the face
      center so the circular crop can be centred on it
    Returns (False, None, None) on any download or API error.
    """
    try:
        resp = await asyncio.to_thread(
            requests.get, img_url, timeout=10, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code >= 400:
            log.info("  Vision skip (HTTP %d — not downloaded): %s", resp.status_code, img_url)
            return False, None, None

        image_bytes = resp.content
        url_lower = img_url.lower().split("?")[0]
        if url_lower.endswith(".png"):
            mime = "image/png"
        elif url_lower.endswith(".webp"):
            mime = "image/webp"
        else:
            mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"

        name_context = f" This photo is a candidate to represent {full_name}." if full_name else ""
        async with sem:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Is this a photo where one person is clearly the primary subject? "
                            f"Answer yes for headshots, portraits, author photos, profile photos, "
                            f"and any photo (including outdoor or full-body shots) where a single "
                            f"person's face is visible and identifiable. Answer no only for group "
                            f"photos, landscapes, logos, illustrations, or photos where no face is "
                            f"clearly discernible.{name_context}\n"
                            f"Answer on exactly three lines:\n"
                            f"HEADSHOT: yes or no\n"
                            f"DESCRIPTION: if yes, one sentence describing approximate age, hair color/"
                            f"style, and any distinctive features; if no, write N/A\n"
                            f"FACE_POSITION: if yes, estimate the face center as CSS object-position "
                            f"values 'X% Y%' where 0% 0% is top-left and 100% 100% is bottom-right "
                            f"(e.g. '50% 25%' for face in upper-center of a landscape photo, "
                            f"'50% 50%' for a centered portrait); if no, write N/A"
                        ),
                    },
                ]}],
            )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        parsed = {}
        for line in text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                parsed[k.strip().upper()] = v.strip()
        is_headshot = parsed.get("HEADSHOT", "no").lower().startswith("y")
        description = parsed.get("DESCRIPTION") if is_headshot else None
        if description and description.upper() in ("N/A", "NA", ""):
            description = None
        face_position = None
        if is_headshot:
            raw_pos = parsed.get("FACE_POSITION", "")
            m = re.search(r'(\d{1,3}%\s+\d{1,3}%)', raw_pos)
            face_position = m.group(1) if m else "50% 25%"
        return is_headshot, description, face_position
    except Exception as exc:
        log.debug("  Vision check failed for %s: %s", img_url, exc)
        return False, None, None


async def _select_by_consistency(
    client: anthropic.AsyncAnthropic,
    sem: asyncio.Semaphore,
    confirmed: list[tuple[int, str, str, str | None, str | None]],
    full_name: str,
) -> tuple[str, str, str | None]:
    """Given multiple confirmed headshots, pick the one most likely to be the correct person.

    Sends Claude all physical descriptions and asks which form a consistent group
    (i.e. appear to be the same individual). Returns (url, source_url, face_position) of the
    highest-scored consistent candidate (confirmed is expected sorted by score descending).
    Falls back to the top candidate on any error.
    """
    if len(confirmed) == 1:
        return confirmed[0][1], confirmed[0][2], confirmed[0][4]

    entries = "\n".join(
        f"{i + 1}. {desc or 'No description available'}"
        for i, (_, _, _src, desc, _fp) in enumerate(confirmed)
    )
    prompt = (
        f"Below are physical descriptions of people shown in {len(confirmed)} different "
        f"photos, each a candidate headshot for {full_name}.\n\n"
        f"{entries}\n\n"
        f"Which numbers describe the same person? Reply with only the numbers, "
        f"comma-separated (e.g. '1, 3'). If every description appears to be a different "
        f"person, reply with '1'."
    )
    try:
        async with sem:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
        raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        indices = [
            int(n) - 1
            for n in re.findall(r'\d+', raw)
            if 0 <= int(n) - 1 < len(confirmed)
        ]
        if not indices:
            indices = [0]
        # confirmed is sorted by score descending; min index = highest score
        best_idx = min(indices)
        log.info(
            "  Consistency check: photos %s are consistent; selecting #%d (score %d): %s",
            [i + 1 for i in indices], best_idx + 1, confirmed[best_idx][0], confirmed[best_idx][1],
        )
        return confirmed[best_idx][1], confirmed[best_idx][2], confirmed[best_idx][4]
    except Exception as exc:
        log.warning("  Consistency selection failed (%s), falling back to top candidate.", exc)
        return confirmed[0][1], confirmed[0][2], confirmed[0][4]


async def _find_photo(
    gemini: genai.Client, guest: dict, hint_url: str
) -> tuple[str | None, str | None]:
    """Search for a publicly accessible profile photo. Returns (photo_url, source_url)."""
    full_name = f"{guest['first_name']} {guest['last_name']}".strip()
    location = _location_str(guest)
    log.info("    Searching for photo via fallback (Twitter/X, company pages, news)…")
    prompt = PHOTO_PROMPT.format(full_name=full_name, location=location, hint_url=hint_url or "none")
    _GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "180"))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                gemini.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.0,
                ),
            ),
            timeout=_GEMINI_TIMEOUT,
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


async def _profile_one(
    gemini: genai.Client,
    claude: anthropic.AsyncAnthropic,
    sem: asyncio.Semaphore,
    scrape_sem: asyncio.Semaphore,
    guest: dict,
) -> dict:
    """Profile a single guest asynchronously."""
    full_name = f"{guest['first_name']} {guest['last_name']}".strip()
    current_guest.set(full_name)
    location = _location_str(guest)
    email = guest.get("email", "")
    email_domain = email.split("@")[-1] if "@" in email else "unknown"
    log.info("Profiling: %s (%s)…", full_name, location or "location unknown")

    # Build existing-context hints from Salesforce data so Gemini focuses on new info.
    existing_context_parts: list[str] = []
    account_title = guest.get("account_title", "")
    account_description = guest.get("account_description", "")
    account_website = guest.get("account_website", "")
    if account_title:
        existing_context_parts.append(
            f"Known title: {account_title} — do not re-research the title, focus on other information."
        )
    if account_description:
        existing_context_parts.append(
            f"Existing context about this person: {account_description} "
            f"— focus on finding genuinely NEW information not already captured above."
        )
    if account_website:
        existing_context_parts.append(f"Known website: {account_website}")
    existing_context = "\n".join(existing_context_parts)

    # 3c: Cache-hit — skip Gemini+Claude pipeline if guest has complete SF profile.
    if account_description and account_title and account_website:
        log.info("Guest %s has complete SF profile — skipping Gemini research.", full_name)
        cached_profile = {
            "summary": account_description[:500],
            "summary_es": "",
            "links": [account_website],
            "photo_url": None,
            "photo_face_position": None,
            "photo_source_url": None,
            "confidence": 10,
            "confidence_reason": "Profile data from Salesforce (cached).",
        }
        result = {**guest, "profile": cached_profile}
        result["has_new_info"] = False
        result["new_title"] = ""
        result["new_bio"] = ""
        result["new_website"] = ""
        result["new_photo_url"] = ""
        return result

    prompt = PROFILE_PROMPT.format(
        full_name=full_name, location=location, email_domain=email_domain,
        existing_context=existing_context,
    )

    # 5 retries for transient malformed JSON; blocked/filtered responses skip retries.
    _MAX_ATTEMPTS = 5
    _NO_RETRY_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII"}
    profile = None
    grounding_urls: list[str] = []
    _GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "180"))
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    gemini.models.generate_content,
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                        temperature=0.0,
                    ),
                ),
                timeout=_GEMINI_TIMEOUT,
            )

            # Check finish reason before trying to parse — a blocked/filtered response
            # will always return empty text, so retrying is pointless.
            try:
                finish_reason = response.candidates[0].finish_reason
                reason_name = finish_reason.name if finish_reason else "STOP"
            except (AttributeError, IndexError):
                reason_name = "STOP"
            if reason_name in _NO_RETRY_REASONS:
                log.warning(
                    "  Profile blocked by Gemini safety filter (%s) for %s — skipping retries.",
                    reason_name, full_name,
                )
                break  # exit retry loop; profile stays None → returns fallback below

            profile = _parse_profile(response.text or "")
            # Extract verified URLs from grounding metadata — these are the pages
            # Gemini actually visited, bypassing text-generation hallucination.
            grounding_urls, queries = _extract_grounding_urls(response)
            if queries:
                log.info("  Gemini searched: %s", " | ".join(queries))
            if grounding_urls:
                log.info("  %d grounding URL(s) from metadata.", len(grounding_urls))
            break  # parsed successfully
        except asyncio.TimeoutError:
            log.warning(
                "  Gemini timed out for %s (attempt %d/%d, %ds limit).",
                full_name, attempt, _MAX_ATTEMPTS, _GEMINI_TIMEOUT,
            )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(min(2 ** attempt, 30))
        except json.JSONDecodeError as exc:
            log.warning(
                "  Profile JSON parse failed for %s (attempt %d/%d): %s",
                full_name, attempt, _MAX_ATTEMPTS, exc,
            )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(min(2 ** attempt, 30))  # back-off, capped at 30s
        except Exception as exc:
            log.error("  Gemini API call failed for %s: %s", full_name, exc)
            err = {**guest, "profile": {
                "summary": "Profile lookup failed due to an API error.",
                "summary_es": "La búsqueda de perfil falló por un error de API.",
                "links": [], "photo_url": None, "photo_source_url": None, "confidence": 0,
                "confidence_reason": "API error during profile lookup.",
            }, "has_new_info": False, "new_title": "", "new_bio": "",
               "new_website": "", "new_photo_url": ""}
            return err

    if profile is None:
        log.error("  Profile JSON could not be parsed for %s after %d attempts.", full_name, _MAX_ATTEMPTS)
        err = {**guest, "profile": {
            "summary": "Profile lookup failed — response could not be parsed.",
            "summary_es": "La búsqueda de perfil falló — la respuesta no pudo procesarse.",
            "links": [], "photo_url": None, "photo_source_url": None, "confidence": 0,
            "confidence_reason": "JSON parse failure after all retries.",
        }, "has_new_info": False, "new_title": "", "new_bio": "",
           "new_website": "", "new_photo_url": ""}
        return err
    log.info(
        "  Profile received — confidence: %d/10 | links: %d | photo: %s",
        profile["confidence"],
        len(profile["links"]),
        "yes" if profile["photo_url"] else "none",
    )

    profile["links"] = await _validate_links(profile["links"])

    name_parts = [p.lower() for p in full_name.split() if len(p) > 2]

    # Resolve ALL grounding redirect wrappers to their actual destinations upfront.
    # Used for both link display AND scraping — scraping the direct URL is more reliable
    # than going through Google's redirect, which can trigger bot-protection challenges
    # on the destination site (e.g. Cloudflare serving a JS challenge instead of HTML).

    async def _resolve_url(u: str) -> str:
        if "grounding-api-redirect" in u:
            return await asyncio.to_thread(_resolve_redirect, u)
        return u

    resolved_grounding: list[str] = list(
        await asyncio.gather(*[_resolve_url(u) for u in grounding_urls])
    ) if grounding_urls else []

    # Supplement displayed links with resolved grounding URLs
    existing_links = {u.rstrip("/") for u in profile["links"]}
    for resolved in resolved_grounding:
        if resolved.rstrip("/") not in existing_links:
            profile["links"].append(resolved)
            existing_links.add(resolved.rstrip("/"))

    # Build a unified candidate pool: Claude's suggestion + everything scraped from links.
    # Both go through the same scoring so the best signal wins regardless of source.
    candidate_pool: list[tuple[int, str, str]] = []

    if profile["photo_url"]:
        score = _score_photo_candidate(profile["photo_url"], name_parts, is_meta_tag=False)
        log.info("  Claude photo suggestion scored %d: %s", score, profile["photo_url"])
        candidate_pool.append((score, profile["photo_url"], "gemini"))

    # Twitter/X: if any link is a twitter.com/x.com profile, resolve the profile image via
    # Twitter's public profile_image redirect (no auth needed; redirects to pbs.twimg.com).
    def _twitter_profile_image(twitter_url: str) -> str | None:
        m = re.match(r'https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]+)/?$', twitter_url)
        if not m:
            return None
        screen_name = m.group(1)
        if screen_name.lower() in ("share", "intent", "hashtag", "search"):
            return None
        try:
            r = requests.head(
                f"https://api.twitter.com/1/users/profile_image?screen_name={screen_name}&size=original",
                timeout=(4, 8), allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"},
            )
            final = r.url
            if "pbs.twimg.com" in final and re.search(r'\.(jpe?g|png|webp)', final, re.IGNORECASE):
                return final
        except Exception:
            pass
        return None

    for link in profile["links"]:
        if re.search(r'(?:twitter|x)\.com/', link, re.IGNORECASE):
            twimg = await asyncio.to_thread(_twitter_profile_image, link)
            if twimg:
                score = _score_photo_candidate(twimg, name_parts, is_meta_tag=False)
                score += 4  # bonus: sourced from their own Twitter profile page
                log.info("  Twitter profile image (score %d): %s", score, twimg)
                candidate_pool.append((score, twimg, link))

    # Pages to scrape: links (includes resolved grounding URLs) + resolved grounding URLs
    # (in case some weren't added to links due to dedup) + email-domain bio pages.
    # Use resolved URLs rather than the original redirect wrappers so we fetch the actual
    # page directly (avoids Cloudflare/bot challenges triggered by Google's redirect service).
    domain_pages = _email_domain_bio_pages(email_domain, full_name)
    name_pages = await asyncio.to_thread(_name_based_domains, full_name)
    # Add confirmed personal website root URLs to profile links for the report
    # (only root domains, not /about /bio subpages which are just scraping targets)
    for np in name_pages:
        parsed_np = urlparse(np)
        if parsed_np.path in ("", "/") and np.rstrip("/") not in existing_links:
            profile["links"].append(np)
            existing_links.add(np.rstrip("/"))
    domain_pages = list(dict.fromkeys(domain_pages + name_pages))  # dedup
    all_pages = list(dict.fromkeys(profile["links"] + resolved_grounding + domain_pages))  # dedup
    if all_pages:
        log.info("  Scraping %d page(s) for photo candidates (%d from email domain)…",
                 len(all_pages), len(domain_pages))
        scraped = await _scrape_photo_candidates_from_links(all_pages, full_name, scrape_sem)
        log.info("  %d scraped candidate(s) found.", len(scraped))
        candidate_pool.extend(scraped)

    # Filter out low-signal candidates, sort best first
    # Score >= 4 required: score 2-3 means only page_bonus fired (name on page, nothing else)
    # — no name in image URL, no profile-signal, no bio-page source. Far too weak a signal;
    # these are typically article author thumbnails or unrelated site assets.
    candidate_pool = [(s, u, src) for s, u, src in candidate_pool if s >= 4]
    candidate_pool.sort(key=lambda x: x[0], reverse=True)
    if candidate_pool:
        log.info("  Top photo candidates: %s", [(s, u) for s, u, _ in candidate_pool[:3]])

    profile["photo_url"] = None
    profile["photo_face_position"] = None
    profile["photo_source_url"] = None
    if candidate_pool:
        top = candidate_pool[:5]
        log.info("  Vision-checking top %d candidate(s)…", len(top))
        checks = await asyncio.gather(
            *[_check_is_headshot(claude, sem, url, full_name) for _, url, _ in top]
        )
        # checks is list of (bool, str | None, str | None) — is_headshot, description, face_position
        confirmed: list[tuple[int, str, str, str | None, str | None]] = [
            (s, u, src, desc, face_pos)
            for (s, u, src), (passed, desc, face_pos) in zip(top, checks)
            if passed
        ]
        for (s, u, _src), (passed, desc, _fp) in zip(top, checks):
            status = "confirmed" if passed else "rejected"
            log.info("  Vision %s (score %d): %s", status, s, u)

        if confirmed:
            if len(confirmed) == 1:
                best_url, best_source, best_face_pos = confirmed[0][1], confirmed[0][2], confirmed[0][4]
                log.info("  1 confirmed headshot (score %d): %s", confirmed[0][0], best_url)
            else:
                log.info("  %d confirmed headshots; running consistency check…", len(confirmed))
                best_url, best_source, best_face_pos = await _select_by_consistency(claude, sem, confirmed, full_name)
            profile["photo_url"] = best_url
            profile["photo_face_position"] = best_face_pos
            profile["photo_source_url"] = best_source
        else:
            log.info("  No candidates passed vision check.")

    # Last resort: dedicated Claude photo search
    if not profile["photo_url"]:
        hint = profile["links"][0] if profile["links"] else ""
        photo_url, source_url = await _find_photo(gemini, guest, hint)
        profile["photo_url"] = photo_url
        profile["photo_source_url"] = source_url
        if source_url and source_url not in profile["links"]:
            if await asyncio.to_thread(_check_link, source_url):
                profile["links"].append(source_url)

    log.info("  Done: %s — confidence: %d/10 | links: %d | photo: %s",
        full_name, profile["confidence"], len(profile["links"]),
        "found" if profile["photo_url"] else "not found",
    )

    # 3b: Detect genuinely new information vs what's already in Salesforce.
    result = {**guest, "profile": profile}
    new_title = ""
    new_bio = ""
    new_website = ""
    new_photo_url = ""

    summary = profile.get("summary", "")
    no_info = summary.lower().startswith("no public information found")

    # New title: extract from summary if SF has no title
    if not account_title and not no_info and summary:
        title_m = re.search(
            r'(?:is|serves as|works as|was)\s+(?:the\s+|a\s+|an\s+)?'
            r'(.{3,60}?\s+(?:at|of|for)\s+.{2,60}?)(?:\.|,|\s+(?:and|who|based|in|with))',
            summary, re.IGNORECASE,
        )
        if title_m:
            new_title = title_m.group(1).strip()

    # New bio: if no existing description and Gemini found something substantive
    if not no_info and summary:
        if not account_description:
            new_bio = summary
        elif len(summary) > 50 and summary.lower() not in account_description.lower():
            new_bio = summary

    # New website: if SF has none, look for a non-social-media link
    _SOCIAL_DOMAINS = {"linkedin.com", "instagram.com", "twitter.com", "x.com",
                       "facebook.com", "tiktok.com", "youtube.com", "threads.net"}
    if not account_website:
        for link in profile.get("links", []):
            link_domain = urlparse(link).netloc.lower().lstrip("www.")
            if link_domain and link_domain not in _SOCIAL_DOMAINS:
                new_website = link
                break

    # New photo
    if profile.get("photo_url"):
        new_photo_url = profile["photo_url"]

    result["new_title"] = new_title
    result["new_bio"] = new_bio
    result["new_website"] = new_website
    result["new_photo_url"] = new_photo_url
    result["has_new_info"] = bool(new_title or new_bio or new_website or new_photo_url)

    if result["has_new_info"]:
        new_fields = [k for k in ("new_title", "new_bio", "new_website", "new_photo_url") if result[k]]
        log.info("  New info detected for %s: %s", full_name, ", ".join(new_fields))

    return result


async def _profile_all(guests: list[dict]) -> list[dict]:
    gemini = _build_gemini_client()
    claude = _build_claude_client()
    # Semaphore gates Claude vision/consistency calls only — they fire in bursts
    # (up to 5 per guest × N guests). Default of 3 is safe; raise LLM_MAX_CONCURRENT
    # if on a higher Claude quota tier.
    max_concurrent = int(os.environ.get("LLM_MAX_CONCURRENT", "3"))
    sem = asyncio.Semaphore(max_concurrent)
    # Separate semaphore for HTTP page fetches — prevents thread pool exhaustion
    # when many guests are profiled simultaneously (each guest scrapes ~10+ pages).
    scrape_concurrent = int(os.environ.get("SCRAPE_MAX_CONCURRENT", "15"))
    scrape_sem = asyncio.Semaphore(scrape_concurrent)
    # Stagger Gemini profile calls to stay within Gemini RPM quota.
    rpm = int(os.environ.get("GEMINI_RPM", "60"))
    delay = 60.0 / rpm
    log.info(
        "Profiling %d guest(s) — Gemini %s (research) + Claude %s (vision) — %d RPM stagger…",
        len(guests), GEMINI_MODEL, CLAUDE_MODEL, rpm,
    )

    async def staggered(i: int, guest: dict) -> dict:
        await asyncio.sleep(i * delay)
        return await _profile_one(gemini, claude, sem, scrape_sem, guest)

    tasks = [staggered(i, g) for i, g in enumerate(guests)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    profiled = []
    for guest, result in zip(guests, results):
        if isinstance(result, BaseException):
            full_name = f"{guest['first_name']} {guest['last_name']}".strip()
            log.error("Profiling task failed for %s: %s", full_name, result, exc_info=result)
            profiled.append({**guest, "profile": {
                "summary": "Profile lookup failed.",
                "summary_es": "La búsqueda de perfil falló.",
                "links": [], "photo_url": None, "photo_source_url": None, "confidence": 0,
                "confidence_reason": "Unexpected error during profiling.",
            }, "has_new_info": False, "new_title": "", "new_bio": "",
               "new_website": "", "new_photo_url": ""})
        else:
            profiled.append(result)

    log.info("All guests profiled.")
    return profiled


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
