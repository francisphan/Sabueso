"""Query Salesforce for TVRS guests currently on property or arriving in the next 7 days."""

import base64
import logging
import os
import requests
from simple_salesforce import Salesforce

log = logging.getLogger(__name__)


def _nested_get(rec: dict | None, *keys: str):
    """Safely traverse nested SF relationship dicts (returns None if any level is None)."""
    val = rec
    for k in keys:
        if val is None or not isinstance(val, dict):
            return None
        val = val.get(k)
    return val

SOQL = """
SELECT Guest_First_Name__c, Guest_Last_Name__c, Email__c,
       Check_In_Date__c, Check_Out_Date__c, Villa_number__c,
       City__c, State_Province__c, Country__c, Language__c,
       Contact__c,
       Contact__r.AccountId,
       Contact__r.Account.Description,
       Contact__r.Account.PersonTitle,
       Contact__r.Account.Website,
       Comments__c
FROM TVRS_Guest__c
WHERE (Check_In_Date__c >= TODAY AND Check_In_Date__c <= NEXT_N_DAYS:7)
   OR (Check_In_Date__c >= LAST_N_DAYS:7 AND Check_In_Date__c < TODAY AND Check_Out_Date__c >= TODAY)
ORDER BY Check_In_Date__c ASC
"""


def _get_access_token() -> tuple[str, str]:
    """Exchange the refresh token for a fresh access token. Returns (access_token, instance_url)."""
    instance_url = os.environ["SF_INSTANCE_URL"]
    log.info("Requesting Salesforce access token from %s…", instance_url)
    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["SF_CLIENT_ID"],
            "client_secret": os.environ["SF_CLIENT_SECRET"],
            "refresh_token": os.environ["SF_REFRESH_TOKEN"],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    resolved_url = data.get("instance_url", instance_url)
    log.info("Access token obtained. Instance URL: %s", resolved_url)
    return data["access_token"], resolved_url


def _connect() -> Salesforce:
    """Authenticate with Salesforce using OAuth2 connected-app tokens."""
    access_token, instance_url = _get_access_token()
    log.info("Connected to Salesforce.")
    return Salesforce(session_id=access_token, instance_url=instance_url)


def _fetch_stay_counts(sf: Salesforce, account_ids: list[str]) -> dict[str, int]:
    """Return a dict mapping account_id → total TVRS_Guest__c reservation count."""
    if not account_ids:
        return {}
    ids_str = ", ".join(f"'{aid}'" for aid in account_ids)
    soql = (
        "SELECT Contact__r.AccountId acctId, COUNT(Id) cnt "
        "FROM TVRS_Guest__c "
        f"WHERE Contact__r.AccountId IN ({ids_str}) "
        "AND Check_Out_Date__c < TODAY "
        "GROUP BY Contact__r.AccountId"
    )
    log.info("Fetching stay counts for %d account(s)…", len(account_ids))
    result = sf.query_all(soql)
    counts: dict[str, int] = {}
    for rec in result.get("records", []):
        counts[rec["acctId"]] = rec["cnt"]
    return counts


def fetch_upcoming_guests() -> list[dict]:
    """Return guests currently on property or arriving in the next 7 days."""
    sf = _connect()
    log.info("Running SOQL query for on-property and upcoming guests…")
    result = sf.query_all(SOQL.strip())
    records = result.get("records", [])
    log.info("Query returned %d record(s).", len(records))

    guests: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for rec in records:
        guest = {
            "first_name": rec.get("Guest_First_Name__c", ""),
            "last_name": rec.get("Guest_Last_Name__c", ""),
            "email": rec.get("Email__c", ""),
            "check_in": rec.get("Check_In_Date__c", ""),
            "check_out": rec.get("Check_Out_Date__c", ""),
            "villa": rec.get("Villa_number__c", ""),
            "city": rec.get("City__c", ""),
            "state": rec.get("State_Province__c", ""),
            "country": rec.get("Country__c", ""),
            "language": rec.get("Language__c", ""),
            "contact_id": rec.get("Contact__c", ""),
            "account_id": _nested_get(rec, "Contact__r", "AccountId") or "",
            "account_description": _nested_get(rec, "Contact__r", "Account", "Description") or "",
            "account_title": _nested_get(rec, "Contact__r", "Account", "PersonTitle") or "",
            "account_website": _nested_get(rec, "Contact__r", "Account", "Website") or "",
            "reservation_comments": rec.get("Comments__c", "") or "",
            "stay_count": 0,
        }
        # Dedup on name + dates: catches duplicates even when city/email
        # differ between Salesforce records (e.g. city "." vs "Las Condes").
        dedup_key = (
            guest["first_name"],
            guest["last_name"],
            guest["check_in"],
            guest["check_out"],
        )
        if dedup_key in seen:
            log.info(
                "  Skipping duplicate: %s %s (%s - %s)",
                guest["first_name"],
                guest["last_name"],
                guest["check_in"],
                guest["check_out"],
            )
            continue
        seen.add(dedup_key)
        log.debug(
            "  Guest: %s %s | check-in: %s | check-out: %s | villa: %s",
            guest["first_name"],
            guest["last_name"],
            guest["check_in"],
            guest["check_out"],
            guest["villa"] or "—",
        )
        guests.append(guest)

    # Populate stay counts from aggregate query
    account_ids = list({g["account_id"] for g in guests if g["account_id"]})
    if account_ids:
        counts = _fetch_stay_counts(sf, account_ids)
        for g in guests:
            if g["account_id"]:
                g["stay_count"] = counts.get(g["account_id"], 0)

    return guests


def update_account(account_id: str, fields: dict) -> None:
    """Update fields on a PersonAccount."""
    sf = _connect()
    log.info("Updating Account %s with fields: %s", account_id, list(fields.keys()))
    sf.Account.update(account_id, fields)


def upload_photo_to_account(
    account_id: str, photo_bytes: bytes, filename: str, mime_type: str
) -> str:
    """Upload photo as ContentVersion + link to Account. Returns ContentDocumentId."""
    sf = _connect()
    b64_data = base64.b64encode(photo_bytes).decode("ascii")

    log.info("Uploading photo '%s' (%s, %d bytes) for Account %s",
             filename, mime_type, len(photo_bytes), account_id)

    # 1. Create ContentVersion
    cv = sf.ContentVersion.create({
        "Title": filename,
        "PathOnClient": filename,
        "VersionData": b64_data,
    })
    content_version_id = cv["id"]

    # 2. Query back for ContentDocumentId
    result = sf.query(
        f"SELECT ContentDocumentId FROM ContentVersion WHERE Id = '{content_version_id}'"
    )
    content_document_id = result["records"][0]["ContentDocumentId"]

    # 3. Link to Account
    sf.ContentDocumentLink.create({
        "ContentDocumentId": content_document_id,
        "LinkedEntityId": account_id,
        "ShareType": "V",
    })

    log.info("Photo linked to Account %s (ContentDocumentId: %s)",
             account_id, content_document_id)
    return content_document_id
