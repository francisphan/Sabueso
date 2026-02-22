"""Build an HTML email report from a list of profiled guest dicts."""

import base64
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

_LOGO_PATH = Path(__file__).parent.parent / "assets" / "sabueso.png"



def _logo_data_uri() -> str:
    """Return the Sabueso mascot as a base64 data URI, or empty string if not found."""
    try:
        data = base64.b64encode(_LOGO_PATH.read_bytes()).decode()
        return f"data:image/png;base64,{data}"
    except FileNotFoundError:
        return ""

STYLES = """
body { margin: 0; padding: 0; background: #f4f4f4; font-family: Arial, sans-serif; color: #333; }
.wrapper { max-width: 700px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; color: #1a1a2e; margin-bottom: 4px; }
.subtitle { font-size: 13px; color: #888; margin-bottom: 28px; }
.card { background: #ffffff; border-radius: 8px; padding: 20px 24px; margin-bottom: 20px;
        box-shadow: 0 1px 4px rgba(0,0,0,.08); }
.card-header { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 12px; }
.photo { width: 96px; height: 96px; border-radius: 50%; object-fit: cover;
         border: 2px solid #e0e0e0; flex-shrink: 0; }
.photo-placeholder { width: 96px; height: 96px; border-radius: 50%;
                     background: #e8eaf6; display: flex; align-items: center;
                     justify-content: center; font-size: 36px; flex-shrink: 0; }
.name { font-size: 18px; font-weight: bold; color: #1a1a2e; margin: 0 0 4px; }
.meta { font-size: 13px; color: #666; margin: 0; line-height: 1.6; }
.summary { font-size: 14px; line-height: 1.65; margin-bottom: 6px; }
.summary-es { font-size: 13px; line-height: 1.65; color: #666; font-style: italic; margin-bottom: 12px; }
.links-label { font-size: 12px; font-weight: bold; text-transform: uppercase;
               letter-spacing: .05em; color: #888; margin-bottom: 4px; }
.links { list-style: none; padding: 0; margin: 0; }
.links li { margin-bottom: 4px; }
.links a { color: #3949ab; font-size: 13px; text-decoration: none; }
.links a:hover { text-decoration: underline; }
.badge { display: inline-block; font-size: 11px; font-weight: bold; padding: 2px 8px;
         border-radius: 10px; margin-left: 8px; vertical-align: middle; }
.on-property { background: #e3f2fd; color: #1565c0; }
.confidence { display: inline-block; font-size: 11px; font-weight: bold; padding: 2px 8px;
              border-radius: 10px; margin-left: 8px; vertical-align: middle; }
.confidence-high { background: #e8f5e9; color: #2e7d32; }
.confidence-med  { background: #fff8e1; color: #f57f17; }
.confidence-low  { background: #ffebee; color: #c62828; }
.confidence-reason { font-size: 11px; color: #999; margin: -8px 0 10px; font-style: italic; }
.empty { text-align: center; padding: 48px 0; color: #999; font-size: 15px; }
.footer { text-align: center; font-size: 11px; color: #bbb; margin-top: 32px; }
.header { display: flex; align-items: center; gap: 20px; margin-bottom: 4px; }
.logo { width: 48px; height: 48px; flex-shrink: 0; }
.info-box { background: #f0f4ff; border-left: 3px solid #3949ab; border-radius: 4px;
            padding: 12px 16px; margin-bottom: 16px; font-size: 13px; color: #444; line-height: 1.6; }
.info-box strong { color: #1a1a2e; }
.trivia { background: #fffbf0; border-left: 3px solid #f9a825; border-radius: 4px;
          padding: 12px 16px; margin-bottom: 28px; font-size: 12px; color: #555; line-height: 1.6; }
.trivia strong { color: #7b5800; }
"""


_PLATFORM_LABELS = {
    "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter / X",
    "x.com": "Twitter / X",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "youtube.com": "YouTube",
    "tiktok.com": "TikTok",
    "threads.net": "Threads",
}


def _platform_label(url: str) -> str:
    """Return a human-readable platform name for a URL, or the bare domain."""
    lower = url.lower()
    for domain, label in _PLATFORM_LABELS.items():
        if domain in lower:
            return label
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or url


def _fmt_date(iso: str) -> str:
    """Format 'YYYY-MM-DD' as 'Mon DD, YYYY'."""
    if not iso:
        return "—"
    try:
        return date.fromisoformat(iso).strftime("%b %d, %Y")
    except ValueError:
        return iso


def _guest_card(guest: dict) -> str:
    profile = guest.get("profile", {})
    full_name = f"{guest.get('first_name', '')} {guest.get('last_name', '')}".strip() or "Unknown Guest"
    photo_url = profile.get("photo_url")
    photo_face_position = profile.get("photo_face_position") or "50% 25%"
    summary = profile.get("summary", "")
    summary_es = profile.get("summary_es", "")
    links = profile.get("links", [])
    confidence = profile.get("confidence", 0)
    confidence_reason = profile.get("confidence_reason", "")
    if confidence >= 8:
        conf_class, conf_label = "confidence-high", f"Confidence: {confidence}/10"
    elif confidence >= 5:
        conf_class, conf_label = "confidence-med", f"Confidence: {confidence}/10"
    else:
        conf_class, conf_label = "confidence-low", f"Confidence: {confidence}/10"

    # Determine if guest is currently on property
    today = date.today().isoformat()
    check_in_raw = guest.get("check_in", "")
    check_out_raw = guest.get("check_out", "")
    on_property = bool(check_in_raw and check_out_raw and check_in_raw < today <= check_out_raw)

    location_parts = [guest.get("city", ""), guest.get("state", ""), guest.get("country", "")]
    location = ", ".join(p for p in location_parts if p) or "—"

    check_in = _fmt_date(check_in_raw)
    check_out = _fmt_date(check_out_raw)
    villa = guest.get("villa", "—") or "—"
    language = guest.get("language", "—") or "—"

    photo_source_url = profile.get("photo_source_url")
    if photo_url:
        photo_img = f'<img src="{photo_url}" alt="{full_name}" class="photo" style="object-position: {photo_face_position}">'
        if photo_source_url and photo_source_url != "gemini":
            source_label = _platform_label(photo_source_url)
            photo_html = (
                f'<div style="flex-shrink:0; text-align:center;">'
                f'{photo_img}'
                f'<div style="margin-top:2px;"><a href="{photo_source_url}" style="font-size:10px; color:#999; text-decoration:none;">via {source_label}</a></div>'
                f'</div>'
            )
        else:
            photo_html = photo_img
    else:
        photo_html = '<div class="photo-placeholder">👤</div>'

    links_html = ""
    if links:
        items = "\n".join(f'<li><a href="{url}">{_platform_label(url)}</a></li>' for url in links)
        links_html = f"""
        <div class="links-label">Links explored/found</div>
        <ul class="links">{items}</ul>
        """

    return f"""
    <div class="card">
      <div class="card-header">
        {photo_html}
        <div>
          <p class="name">{full_name} {'<span class="badge on-property">On Property</span>' if on_property else ''}<span class="confidence {conf_class}">{conf_label}</span></p>
          <p class="meta">
            Check-in: {check_in} &nbsp;|&nbsp; Check-out: {check_out}<br>
            Villa: {villa} &nbsp;|&nbsp; Language: {language}<br>
            Location: {location}
          </p>
        </div>
      </div>
      {f'<p class="confidence-reason">{confidence_reason}</p>' if confidence_reason else ''}
      <p class="summary">{summary}</p>
      {f'<p class="summary-es">{summary_es}</p>' if summary_es else ''}
      {links_html}
    </div>
    """


def build_html(guests_with_profiles: list[dict]) -> str:
    """Render a full HTML email from a list of profiled guest dicts."""
    today = date.today().strftime("%B %d, %Y")
    logo_uri = _logo_data_uri()
    logo_html = f'<img src="{logo_uri}" alt="Sabueso" class="logo">' if logo_uri else ""

    if guests_with_profiles:
        cards = "\n".join(_guest_card(g) for g in guests_with_profiles)
        body_html = cards
    else:
        body_html = '<p class="empty">No guests arriving in the next 7 days.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sabueso Anticipatory Guest Report</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      {logo_html}
      <h1>Sabueso Anticipatory Guest Report</h1>
    </div>
    <p class="subtitle">On property &amp; arriving in the next 7 days &mdash; generated {today}</p>
    <div class="info-box">
      <strong>About this report</strong><br>
      This is a bi-weekly automated briefing sent every Monday and Thursday at 08:00 ART.
      It covers all guests <strong>currently on property</strong> (checked in within the past 7 days)
      and guests <strong>arriving within the next 7 days</strong>. Guest profiles are researched
      automatically using Gemini with Google Search grounding — confidence scores reflect how
      reliably the correct individual could be identified from public sources. Do not treat
      low-confidence profiles as verified.<br><br>
      <strong>Photo disclaimer:</strong> Profile photos are sourced automatically from publicly
      accessible web pages and <strong>may not always be accurate</strong>. Due to authentication
      restrictions, images from LinkedIn and Instagram cannot be retrieved. If a photo looks wrong
      or is missing, search the guest&rsquo;s name directly &mdash; their LinkedIn or Instagram
      profile is often the most reliable source.
    </div>
    <div class="trivia">
      <strong>Sabueso</strong> &mdash; <em>bloodhound</em>. A dog bred to track scents
      across long distances, renowned for its relentless nose and ability to follow a trail no matter
      how cold. Here, Sabueso hunts down public information about arriving guests so staff at
      The Vines of Mendoza can greet every visitor with a personal touch.
    </div>
    {body_html}
    <p class="footer">This report is auto-generated by Sabueso. Do not reply.</p>
  </div>
</body>
</html>"""
