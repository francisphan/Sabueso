"""
One-off ACL bulk import.

Looks up Slack user IDs for a list of emails via users.lookupByEmail
and merges them into acl.json as read_only.

Usage:
    SLACK_BOT_TOKEN=xoxb-... python scripts/bulk_import_acl.py

Existing entries in acl.json are preserved. Re-running is idempotent —
users already present at any role keep their current role.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

EMAILS = [
    # Concierge / front-of-house
    "pilar.manzano@vinesofmendoza.com",
    "concierge@vinesresortandspa.com",
    "agustina.ramos@vinesofmendoza.com",
    "josefina.rivas@vinesofmendoza.com",
    "eugenia.lucero@vinesofmendoza.com",
    "martina.ferrari@vinesofmendoza.com",
    # Client Services
    "ana.roibon@vinesofmendoza.com",
    "camila.oviedo@vinesofmendoza.com",
    "candy.kermen@vinesofmendoza.com",
    "corina.centeno@vinesofmendoza.com",
    "enrique.roman@vinesofmendoza.com",
    "fernanda.zurita@vinesofmendoza.com",
    "lucia.linde@vinesofmendoza.com",
    "macarena.gimenez@vinesofmendoza.com",
    # L10
    "carla.auger@vinesofmendoza.com",
    "gonzalo.robredo@vinesofmendoza.com",
    "juan.gambino@vinesofmendoza.com",
    "martin.cebrian@vinesofmendoza.com",
    "michael@vinesofmendoza.com",
    "patricio.watson@vinesofmendoza.com",
    # IT
    "carlos.ferrer@vinesofmendoza.com",
    "leandro.villavicencio@vinesofmendoza.com",
    "paula.orrego@vinesofmendoza.com",
    # Ventas
    "brenda.carrion@vinesofmendoza.com",
    "cesar.daste@vinesofmendoza.com",
    "christian.stoddart@vinesofmendoza.com",
]

ACL_PATH = Path(os.getenv("ACL_FILE", "acl.json"))
DEFAULT_ROLE = "read_only"


def main() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("error: SLACK_BOT_TOKEN must be set", file=sys.stderr)
        return 1

    client = WebClient(token=token)

    acl: dict[str, str] = {}
    if ACL_PATH.exists():
        acl = json.loads(ACL_PATH.read_text())
        print(f"Loaded existing ACL with {len(acl)} entries from {ACL_PATH}")
    else:
        print(f"No existing {ACL_PATH} — creating fresh")

    added: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for email in EMAILS:
        try:
            resp = client.users_lookupByEmail(email=email)
            user_id = resp["user"]["id"]
        except SlackApiError as e:
            failed.append((email, e.response.get("error", str(e))))
            continue

        if user_id in acl:
            skipped.append((email, f"{user_id} already has role {acl[user_id]}"))
            continue

        acl[user_id] = DEFAULT_ROLE
        added.append((email, user_id))

    ACL_PATH.write_text(json.dumps(acl, indent=2, sort_keys=True))

    print(f"\nWrote {len(acl)} entries to {ACL_PATH}\n")
    print(f"Added ({len(added)}):")
    for email, uid in added:
        print(f"  {email} → {uid}")
    print(f"\nSkipped — already in ACL ({len(skipped)}):")
    for email, reason in skipped:
        print(f"  {email}: {reason}")
    print(f"\nFailed ({len(failed)}):")
    for email, reason in failed:
        print(f"  {email}: {reason}")

    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
