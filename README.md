# Sabueso — Anticipatory Guest Report

<img src="assets/sabueso.png" alt="Sabueso mascot" width="96">

> **Sabueso** (Spanish) — *bloodhound*. A dog bred to track scents across long distances, renowned for its relentless nose and ability to follow a trail no matter how cold. Here, Sabueso hunts down public information about arriving guests so staff at The Vines of Mendoza can greet every visitor with a personal touch.

Sabueso automatically generates a guest intelligence briefing for [The Vines of Mendoza](https://www.vinesofmendoza.com/). Every Monday and Thursday at 08:00 UTC it:

1. Queries Salesforce for guests **currently on property** and **arriving in the next 7 days**
2. Researches each guest using **Gemini 2.5 Flash** with Google Search grounding
3. Finds the best publicly available headshot using **Claude Sonnet** vision checks
4. Builds an **HTML email** with per-guest profile cards (bio, Spanish translation, social links, photo, confidence score)
5. Sends the report to a configurable subscriber list via Gmail

---

## Project Structure

```
src/
  main.py               CLI entry point
  salesforce_client.py  Salesforce OAuth2 + SOQL query
  gemini_profiler.py    Gemini research + Claude vision photo pipeline
  report_builder.py     HTML email renderer
  email_sender.py       Gmail API sender
  scheduler.py          Pipeline orchestration (fetch → profile → build → send)
scripts/
  get_salesforce_token.py   One-time OAuth flow to get SF_REFRESH_TOKEN
  get_gmail_token.py        One-time OAuth flow to get GMAIL_REFRESH_TOKEN
.github/workflows/
  guest-report.yml      GitHub Actions cron (Mon/Thu 08:00 UTC)
```

---

## How It Works

### Pipeline (`scheduler.py`)

```
Salesforce SOQL
      ↓
fetch_upcoming_guests()   → list of guest dicts
      ↓
profile_guests()          → Gemini researches each guest concurrently
      ↓                     Claude vision-checks candidate photos
build_html()              → HTML email with one card per guest
      ↓
send_report()             → Gmail API sends to REPORT_SUBSCRIBERS
```

### Salesforce Query (`salesforce_client.py`)

Fetches from `TVRS_Guest__c` using an OAuth2 refresh-token flow (no username/password). Returns guests who:
- Are **arriving** in the next 7 days, **or**
- Are **currently on property** and checked in within the last 7 days

### Gemini Profiling (`gemini_profiler.py`)

Each guest goes through two phases:

**Phase 1 — Research (Gemini 2.5 Flash + Google Search grounding)**
- Returns English bio, Spanish bio, all social/professional links, a confidence score (0–10)
- Grounding metadata URLs (pages Gemini actually visited) are resolved and merged into links
- Retries up to 5 times on JSON parse failure; backs off exponentially

**Phase 2 — Photo selection**
1. **Scrape candidates** from all known links + email-domain bio pages (e.g. `/team`, `/about`, `/team-member/first-last`). Follows discovered internal bio/team links one level deep.
2. **Score** each candidate image using URL signals, page context, og:image tags, JSON-LD Person schema, alt/title attributes, name proximity in HTML, and penalties for banners/landscapes/article slugs.
3. **Vision-check top 5** via Claude Sonnet — downloads the image and confirms it is a headshot of a single person.
4. **Consistency check** — if multiple candidates pass, Claude compares their physical descriptions and selects the self-consistent group, returning the highest-scored matching photo.
5. **Fallback** — if no candidate passes, a dedicated Gemini search looks for a photo on Twitter/X or news sites.

> **Photo disclaimer:** Photos are sourced automatically and may not always be accurate. LinkedIn and Instagram photos cannot be retrieved without authentication. If a photo looks wrong, search the guest's name directly.

### Report Card Fields

Each guest card shows:
- Circular headshot (96px) or placeholder avatar
- Full name with **On Property** badge and color-coded **Confidence** badge (green ≥ 8, yellow ≥ 5, red < 5)
- Check-in / check-out / villa / language / home location
- Confidence reasoning note
- English bio (2–4 sentences)
- Spanish bio
- Validated links (social profiles, company bios, news articles)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in all values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio API key (Gemini 2.5 Flash) |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude Sonnet — vision checks) |
| `SF_CLIENT_ID` | Salesforce Connected App consumer key |
| `SF_CLIENT_SECRET` | Salesforce Connected App consumer secret |
| `SF_REFRESH_TOKEN` | Salesforce OAuth2 refresh token |
| `SF_INSTANCE_URL` | Salesforce instance URL (e.g. `https://yourorg.salesforce.com`) |
| `GMAIL_CLIENT_ID` | Google OAuth2 client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth2 client secret |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth2 refresh token |
| `GMAIL_SENDER` | Gmail address to send from |
| `REPORT_SUBSCRIBERS` | Comma-separated recipient email addresses |

**Optional tuning:**

| Variable | Default | Description |
|---|---|---|
| `GEMINI_RPM` | `60` | Gemini requests per minute (stagger between guests) |
| `GEMINI_TIMEOUT` | `180` | Seconds before a Gemini call is cancelled |
| `GMAIL_TIMEOUT` | `60` | Seconds before Gmail API calls time out |
| `LLM_MAX_CONCURRENT` | `3` | Max simultaneous Claude vision calls |
| `SCRAPE_MAX_CONCURRENT` | `15` | Max simultaneous page fetches |

### 3. Get OAuth tokens

**Salesforce refresh token:**
```bash
python scripts/get_salesforce_token.py
```
Opens a browser for the Salesforce OAuth flow. Requires the Connected App to have `full` and `offline_access` scopes and `http://localhost:8402/callback` as an allowed callback URL.

**Gmail refresh token:**
```bash
python scripts/get_gmail_token.py
```
Opens a browser for the Google OAuth flow. Requires a Google Cloud OAuth2 client with the Gmail Send scope.

---

## Running Locally

Run the report immediately:
```bash
python src/main.py --now
```

Override recipients for testing:
```bash
python src/main.py --now --to you@example.com
```

Run multiple times against the same Salesforce data (cached after first fetch):
```bash
python src/main.py --now --runs 3 --to you@example.com
```

Start the Monday/Thursday scheduler (blocks indefinitely):
```bash
python src/main.py
```

---

## GitHub Actions (Production)

The workflow in `.github/workflows/guest-report.yml` runs `python src/main.py --now` on a cron schedule:

| Day | Cron (UTC) | Local time (ART, UTC-3) |
|---|---|---|
| Monday | `0 8 * * 1` | 05:00 |
| Thursday | `0 8 * * 4` | 05:00 |

It can also be triggered manually from the **Actions** tab via `workflow_dispatch`.

### Required GitHub Secrets

```bash
gh secret set GEMINI_API_KEY
gh secret set ANTHROPIC_API_KEY
gh secret set SF_CLIENT_ID
gh secret set SF_CLIENT_SECRET
gh secret set SF_REFRESH_TOKEN
gh secret set SF_INSTANCE_URL
gh secret set GMAIL_CLIENT_ID
gh secret set GMAIL_CLIENT_SECRET
gh secret set GMAIL_REFRESH_TOKEN
gh secret set GMAIL_SENDER
gh secret set REPORT_SUBSCRIBERS
```
