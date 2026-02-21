"""Query Salesforce for TVRS guests arriving in the next 7 days."""

import os
import requests
from simple_salesforce import Salesforce

SOQL = """
SELECT Guest_First_Name__c, Guest_Last_Name__c, Email__c,
       Check_In_Date__c, Check_Out_Date__c, Villa_number__c,
       City__c, State_Province__c, Country__c, Language__c
FROM TVRS_Guest__c
WHERE Check_In_Date__c >= TODAY
  AND Check_In_Date__c <= NEXT_N_DAYS:7
ORDER BY Check_In_Date__c ASC
"""


def _get_access_token() -> tuple[str, str]:
    """Exchange the refresh token for a fresh access token. Returns (access_token, instance_url)."""
    instance_url = os.environ["SF_INSTANCE_URL"]
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
    return data["access_token"], data.get("instance_url", instance_url)


def _connect() -> Salesforce:
    """Authenticate with Salesforce using OAuth2 connected-app tokens."""
    access_token, instance_url = _get_access_token()
    return Salesforce(session_id=access_token, instance_url=instance_url)


def fetch_upcoming_guests() -> list[dict]:
    """Return a list of guest dicts for arrivals in the next 7 days."""
    sf = _connect()
    result = sf.query_all(SOQL.strip())
    records = result.get("records", [])

    guests = []
    for rec in records:
        guests.append(
            {
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
            }
        )
    return guests
