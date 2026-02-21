"""One-time script to obtain a Gmail OAuth refresh token.

Run this once, copy the printed refresh token into .env as GMAIL_REFRESH_TOKEN,
then you never need to run this again.

Usage:
    python scripts/get_gmail_token.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

client_config = {
    "installed": {
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=8401)

print("\n✓ Authorization complete!\n")
print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
print("\nAdd the line above to your .env file.")
