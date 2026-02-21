# Sabueso — Anticipatory Guest Report

<img src="assets/sabueso.png" alt="Sabueso mascot" width="96">

> **Sabueso** (Spanish) — *bloodhound*. A dog bred to track scents across long distances, renowned for its relentless nose and ability to follow a trail no matter how cold. Here, Sabueso hunts down public information about arriving guests so staff at The Vines of Mendoza can greet every visitor with a personal touch.

Sabueso automatically generates a guest intelligence briefing for [The Vines of Mendoza](https://www.vinesofmendoza.com/). Every Monday and Thursday at 08:00 ART it:

1. Queries Salesforce for guests **currently on property** (checked in within the last 7 days) and **arriving in the next 7 days**
2. Researches each guest using **Gemini 2.5 Flash** with Google Search grounding
3. Builds an **HTML email** with per-guest profile cards (bio, Spanish translation, social links, photo, confidence score)
4. Sends the report to a configurable subscriber list via Gmail

---

## Project Structure

```
src/
  main.py               CLI entry point
  salesforce_client.py  Salesforce OAuth2 + SOQL query
  gemini_profiler.py    Gemini research with Google Search grounding
  report_builder.py     HTML email renderer
  email_sender.py       Gmail API sender
  scheduler.py          Pipeline orchestration (fetch → profile → build → send)
scripts/
  get_salesforce_token.py   One-time OAuth flow to get SF_REFRESH_TOKEN
  get_gmail_token.py        One-time OAuth flow to get GMAIL_REFRESH_TOKEN
.github/workflows/
  guest-report.yml      GitHub Actions cron (Mon/Thu 13:00 UTC = 08:00 ART)
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
      ↓
build_html()              → HTML email with one card per guest
      ↓
send_report()             → Gmail API sends to REPORT_SUBSCRIBERS
```

### Salesforce Query (`salesforce_client.py`)

Fetches from `TVRS_Guest__c` using an OAuth2 refresh-token flow (no username/password). Returns guests who:
- Are **arriving** in the next 7 days, **or**
- Are **currently on property** and checked in within the last 7 days

### Gemini Profiling (`gemini_profiler.py`)

Each guest gets two Gemini calls (with Google Search grounding):
1. **Profile call** — returns English bio, Spanish bio, all social/professional links, a confidence score (0–10), and a photo URL
2. **Photo fallback call** — if the first call yields no usable photo, searches Twitter/X, company pages, and news articles for a publicly accessible headshot

All returned links and photos are validated (HTTP HEAD with GET fallback) before inclusion. Requests are staggered according to `GEMINI_RPM` (default: 60) to respect API rate limits.

### Report Card Fields

Each guest card shows:
- Circular headshot (96px) or placeholder avatar
- Full name with **On Property** badge (if currently staying) and color-coded **Confidence** badge (green ≥ 8, yellow ≥ 5, red < 5)
- Check-in / check-out / villa / language / home location
- Confidence reasoning note
- English bio (2–4 sentences)
- Spanish bio
- Validated social and professional links

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
| `GEMINI_API_KEY` | Google AI Studio API key (billing required for Gemini 2.5 Flash) |
| `SF_CLIENT_ID` | Salesforce Connected App consumer key |
| `SF_CLIENT_SECRET` | Salesforce Connected App consumer secret |
| `SF_REFRESH_TOKEN` | Salesforce OAuth2 refresh token |
| `SF_INSTANCE_URL` | Salesforce instance URL (e.g. `https://yourorg.salesforce.com`) |
| `GMAIL_CLIENT_ID` | Google OAuth2 client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth2 client secret |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth2 refresh token |
| `GMAIL_SENDER` | Gmail address to send from |
| `REPORT_SUBSCRIBERS` | Comma-separated recipient email addresses |
| `GEMINI_RPM` | *(optional)* Gemini requests per minute, default `60` |

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

Run the report immediately (for testing):
```bash
python src/main.py --now
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
| Monday | `0 13 * * 1` | 08:00 |
| Thursday | `0 13 * * 4` | 08:00 |

It can also be triggered manually from the **Actions** tab via `workflow_dispatch`.

### Required GitHub Secrets

Set all `.env` variables as repository secrets:

```bash
gh secret set GEMINI_API_KEY
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
