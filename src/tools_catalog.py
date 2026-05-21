"""
Tool catalog for Sabueso.

Each entry maps to an MCP server operation and is formatted as a Claude API
tool definition (JSON-schema style).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Write-operation names — triggers confirmation flow before execution.
# ---------------------------------------------------------------------------
WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "sf_create_record",
        "sf_update_record",
        "sf_delete_record",
        "sf_upsert_record",
        "sf_bulk_operation",
        "ns_create_record",
        "ns_update_record",
        "ns_delete_record",
        "ns_upsert_record",
        "pardot_create_prospect",
        "pardot_update_prospect",
        "pardot_delete_prospect",
        "pardot_upsert_prospect",
        # Sabueso-local intent tools (run via sf_intents, not MCP)
        "sf_create_opportunity_for_person",
        "sf_log_touch",
    }
)

# ---------------------------------------------------------------------------
# Intent tools — Sabueso-local Python functions that the dispatcher routes
# to sf_intents instead of mcp_client.call_tool. Maps tool_name -> sf_intents
# function name. These all live in WRITE_OPERATIONS.
# ---------------------------------------------------------------------------
INTENT_TOOLS: dict[str, str] = {
    "sf_create_opportunity_for_person": "create_opportunity_for_person",
    "sf_log_touch": "log_touch",
}

# Allowed values shared with the tool schemas below and the sf_intents module.
OPPORTUNITY_PRODUCTS: tuple[str, ...] = (
    "TVOM Vineyards",
    "TVOM Housing Lot",
    "TVOM Villa Expansion",
    "TVG Membership",
)

TOUCH_SUBJECTS: tuple[str, ...] = (
    "Call",
    "Conversation",
    "Meetings",
    "Text Message",
    "Voice Mail",
    "Vineyard Visit",
    "Other",
)

# ---------------------------------------------------------------------------
# Claude API tool definitions
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    # ── Sales-rep write intents ─────────────────────────────────────────
    {
        "name": "sf_create_opportunity_for_person",
        "description": (
            "Create a new Salesforce Opportunity for a person identified by "
            "name or email. The requesting Slack user is set as the Owner. "
            "Use when a sales rep describes a new prospect they want to track. "
            "Optionally logs touches (Tasks) for interactions already had."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person_hint": {
                    "type": "string",
                    "description": "Person's name and/or email — used as a SOSL search hint.",
                },
                "product": {
                    "type": "string",
                    "enum": list(OPPORTUNITY_PRODUCTS),
                    "description": "Map the rep's words: vineyard -> TVOM Vineyards, housing lot/build a home -> TVOM Housing Lot, villa expansion -> TVOM Villa Expansion, membership/TVG -> TVG Membership.",
                },
                "notes": {
                    "type": "string",
                    "description": "Free-text context to put in the Opportunity Description.",
                },
                "lead_source": {
                    "type": "string",
                    "description": "Where the lead came from (referral, stayed at TVRS, etc.). Only include if the rep specified — do not invent.",
                },
                "touches": {
                    "type": "array",
                    "description": "Touches already had with the person (to be logged as Tasks).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {
                                "type": "string",
                                "enum": list(TOUCH_SUBJECTS),
                            },
                            "notes": {"type": "string"},
                            "when": {
                                "type": "string",
                                "description": "ISO date or 'today'. Defaults to today.",
                            },
                        },
                        "required": ["subject"],
                    },
                },
            },
            "required": ["person_hint", "product"],
        },
    },
    {
        "name": "sf_log_touch",
        "description": (
            "Log a touch (Task) on an existing open Salesforce Opportunity for "
            "a person. Use for follow-up interactions ('talked to John again')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "opp_hint": {
                    "type": "string",
                    "description": "Opportunity SF ID or name fragment, if known.",
                },
                "person_hint": {
                    "type": "string",
                    "description": "Person's name and/or email — used when opp_hint not given.",
                },
                "subject": {
                    "type": "string",
                    "enum": list(TOUCH_SUBJECTS),
                },
                "notes": {"type": "string"},
                "when": {
                    "type": "string",
                    "description": "ISO date or 'today'. Defaults to today.",
                },
            },
            "required": ["subject"],
        },
    },
    # ── Person/guest lookup ─────────────────────────────────────────────
    {
        "name": "sf_find_by_email",
        "description": (
            "Find all Salesforce records for a person by email. Searches Contact, "
            "Account (Person Account), Lead, and TVRS_Guest__c in one call. "
            "This is the PRIMARY tool for looking up a person in Salesforce. "
            "Use this instead of sf_soql_query when looking up a person by email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Email address to search for.",
                },
            },
            "required": ["email"],
        },
    },
    # ── Cross-system lookups ──────────────────────────────────────────────
    {
        "name": "guest_360_profile",
        "description": (
            "Retrieve a full 360-degree guest profile across Salesforce, NetSuite, "
            "and Pardot for the given email address."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest email address to look up.",
                },
            },
            "required": ["email"],
        },
    },
    {
        "name": "lookup_guest_by_email",
        "description": (
            "Quick cross-system lookup of a guest by email. Returns matching "
            "records from Salesforce, NetSuite, and Pardot without the full 360 "
            "enrichment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest email address.",
                },
            },
            "required": ["email"],
        },
    },
    # ── Salesforce ────────────────────────────────────────────────────────
    {
        "name": "sf_soql_query",
        "description": (
            "Execute a SOQL query against Salesforce. Use this for structured "
            "queries such as listing records, filtering by date ranges, aggregating "
            "counts, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_str": {
                    "type": "string",
                    "description": "A valid SOQL query string.",
                },
            },
            "required": ["query_str"],
        },
    },
    {
        "name": "sf_get_record",
        "description": "Retrieve a single Salesforce record by object type and record ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "object_name": {
                    "type": "string",
                    "description": "Salesforce object API name (e.g. Account, Contact).",
                },
                "record_id": {
                    "type": "string",
                    "description": "The 15- or 18-character Salesforce record ID.",
                },
            },
            "required": ["object_name", "record_id"],
        },
    },
    {
        "name": "sf_search",
        "description": (
            "Perform a SOSL search across Salesforce. Good for free-text / "
            "fuzzy searches when the user doesn't specify exact field values. "
            "Use sosl_query for a full SOSL string (e.g. \"FIND {John Smith} "
            "IN ALL FIELDS RETURNING Contact, Lead, Account\")."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sosl_query": {
                    "type": "string",
                    "description": "Full SOSL query string.",
                },
            },
            "required": ["sosl_query"],
        },
    },
    {
        "name": "sf_list_objects",
        "description": "List all available Salesforce object types.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "sf_describe_object",
        "description": (
            "Describe the schema (fields, relationships) of a Salesforce object."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_name": {
                    "type": "string",
                    "description": "Salesforce object API name.",
                },
            },
            "required": ["object_name"],
        },
    },
    # ── NetSuite ──────────────────────────────────────────────────────────
    {
        "name": "ns_suiteql_query",
        "description": (
            "Execute a SuiteQL query against NetSuite. Use for invoices, sales "
            "orders, purchase orders, journal entries, and any structured query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A valid SuiteQL query string.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 1000).",
                    "default": 1000,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "ns_rest_list",
        "description": (
            "List NetSuite records of a given type. Supports optional filtering "
            "via the q parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "description": "NetSuite record type (e.g. invoice, salesOrder, customer).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (default 100).",
                    "default": 100,
                },
                "q": {
                    "type": "string",
                    "description": "Optional filter query string.",
                },
            },
            "required": ["record_type"],
        },
    },
    {
        "name": "ns_rest_get",
        "description": "Retrieve a single NetSuite record by type and internal ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "description": "NetSuite record type.",
                },
                "record_id": {
                    "type": "string",
                    "description": "NetSuite internal ID.",
                },
            },
            "required": ["record_type", "record_id"],
        },
    },
    {
        "name": "ns_get_netsuite_schema",
        "description": (
            "Get schema information for NetSuite record types. Pass an empty "
            "string to list all available types, or a comma-separated list of "
            "record type names for detailed schema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_types": {
                    "type": "string",
                    "description": (
                        "Comma-separated record type names, or empty string for overview."
                    ),
                    "default": "",
                },
            },
        },
    },
    # ── Pardot ────────────────────────────────────────────────────────────
    {
        "name": "pardot_query_prospects",
        "description": (
            "Query Pardot prospects (marketing leads). Supports field selection, "
            "ordering, and limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "string",
                    "description": "Comma-separated field names to return.",
                    "default": "",
                },
                "order_by": {
                    "type": "string",
                    "description": "Field to order results by.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum prospects to return (default 200).",
                    "default": 200,
                },
            },
        },
    },
    {
        "name": "pardot_query_visitor_activities",
        "description": (
            "Query Pardot visitor activity records. Can filter by prospect ID, "
            "activity type, and date range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {
                    "type": "string",
                    "description": "Filter to activities for this prospect ID.",
                    "default": "",
                },
                "type": {
                    "type": "string",
                    "description": "Activity type filter.",
                    "default": "",
                },
                "created_after": {
                    "type": "string",
                    "description": "ISO-8601 date; only activities created after this.",
                    "default": "",
                },
                "created_before": {
                    "type": "string",
                    "description": "ISO-8601 date; only activities created before this.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (default 200).",
                    "default": 200,
                },
            },
        },
    },
    {
        "name": "pardot_query_lists",
        "description": "Query Pardot marketing lists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "string",
                    "description": "Comma-separated field names.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lists to return (default 200).",
                    "default": 200,
                },
            },
        },
    },
    {
        "name": "pardot_get_prospect",
        "description": "Retrieve a single Pardot prospect by prospect ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {
                    "type": "string",
                    "description": "Pardot prospect ID.",
                },
            },
            "required": ["prospect_id"],
        },
    },
]
