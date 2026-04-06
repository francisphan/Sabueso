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
    }
)

# ---------------------------------------------------------------------------
# Claude API tool definitions
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
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
            "fuzzy searches when the user doesn't specify exact field values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_str": {
                    "type": "string",
                    "description": "SOSL search expression.",
                },
            },
            "required": ["search_str"],
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
