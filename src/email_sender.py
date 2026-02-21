"""Send HTML emails via Gmail API with OAuth 2.0."""

import base64
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_CACHE = Path(__file__).parent.parent / ".gmail_token.json"


def _get_credentials() -> Credentials:
    """Get Gmail OAuth credentials, refreshing or prompting as needed."""
    creds = None

    # Try env vars first (CI path: refresh token as GitHub Secret)
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    if refresh_token:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds

    # Try cached token file (local dev path)
    if TOKEN_CACHE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_CACHE), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    # Full OAuth browser flow (local only)
    client_id = os.environ["GMAIL_CLIENT_ID"]
    client_secret = os.environ["GMAIL_CLIENT_SECRET"]
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=8401)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials):
    """Cache credentials to disk for reuse."""
    TOKEN_CACHE.write_text(creds.to_json())


def send_report(
    subject: str,
    html_body: str,
    recipients: list[str],
    cc: list[str] | None = None,
) -> None:
    """Send an HTML email to all recipients via Gmail API."""
    sender = os.environ.get("GMAIL_SENDER", "me")
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)

    plain_text = "This report is best viewed in an HTML-capable email client."
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    all_recipients = recipients + (cc or [])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    print(f"Report sent to {len(all_recipients)} recipient(s).")
