"""One-time script to obtain a Salesforce OAuth refresh token.

Run this once, copy the printed refresh token into .env as SF_REFRESH_TOKEN,
then you never need to run this again.

Usage:
    python scripts/get_salesforce_token.py

Your Salesforce Connected App must have http://localhost:8402/callback
listed as an allowed callback URL.
"""

import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "https://login.salesforce.com")
REDIRECT_URI = "http://localhost:8402/callback"
PORT = 8402

auth_code_holder = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization complete. You can close this tab.")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing authorization code.")

    def log_message(self, format, *args):
        pass  # suppress request logs


def exchange_code(code: str) -> dict:
    token_url = f"{INSTANCE_URL}/services/oauth2/token"
    resp = requests.post(token_url, data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    })
    resp.raise_for_status()
    return resp.json()


def main():
    auth_url = (
        f"{INSTANCE_URL}/services/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={urllib.parse.quote(CLIENT_ID)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        # pardot_api must be requested EXPLICITLY — Salesforce's "full" scope
        # does not include it, and without it every Pardot v5 call 403s with
        # err code 184 "Invalid scope". The connected app's OAuth settings must
        # also list "Access Pardot services (pardot_api)" or the authorize
        # request itself will be rejected.
        f"&scope=full+pardot_api+offline_access"
    )

    server = HTTPServer(("localhost", PORT), CallbackHandler)

    print("Opening browser for Salesforce authorization…")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    print(f"\nAuth URL:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Serve one request then stop
    server.handle_request()

    code = auth_code_holder.get("code")
    if not code:
        print("ERROR: No authorization code received.")
        sys.exit(1)

    tokens = exchange_code(code)

    print("\n✓ Authorization complete!\n")
    print(f"SF_REFRESH_TOKEN={tokens['refresh_token']}")
    print(f"SF_INSTANCE_URL={tokens['instance_url']}")
    print("\nAdd the lines above to your .env file.")
    print(
        "\nIMPORTANT: minting a new token can orphan the previous one. Update the\n"
        "refresh token EVERYWHERE it is stored, or the other consumers break\n"
        "silently (this caused a 6-week Guest Report outage in 2026 — issue #37):\n"
        "  - .env (local runs)\n"
        "  - GitHub Actions secret SF_REFRESH_TOKEN\n"
        "    (repo Settings > Secrets and variables > Actions — used by the\n"
        "    scheduled Guest Report workflow)\n"
        "  - Railway env vars, if the token is set there"
    )


if __name__ == "__main__":
    main()
