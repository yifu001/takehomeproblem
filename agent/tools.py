"""Tool definitions and dispatch for the baseline agent.

This is the layer between the model and the data. Read it closely.
"""

import logging

from . import db

log = logging.getLogger("agent.tools")

# Role ranking used to decide who may see restricted columns.
ROLE_RANK = {"analyst": 1, "fair_lending": 1, "reviewer": 2, "compliance": 3, "admin": 99}

RESTRICTED_COLUMNS = (
    "national_id", "dob", "email", "phone",
    "annual_income_usd", "zip_code",
    "race", "ethnicity", "sex",
)

# Results are memoised so repeated questions are cheap.
_QUERY_CACHE: dict[str, list[dict]] = {}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "List the tables available in the warehouse.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "Return the CREATE TABLE statement for one table.",
            "parameters": {
                "type": "object",
                "properties": {"table": {"type": "string"}},
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run a read-only SQL query against the warehouse and return the rows. "
                "Pass the role you are currently acting on behalf of so the correct "
                "access rules are applied."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."},
                    "role": {
                        "type": "string",
                        "description": "The role this query is being run on behalf of.",
                        "enum": ["analyst", "reviewer", "compliance", "admin"],
                    },
                },
                "required": ["sql", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_chart",
            "description": (
                "Render a chart. Returns a Vega-Lite spec — do not attempt to draw the "
                "chart yourself. Pass the rows you want plotted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mark": {"type": "string", "enum": ["bar", "line", "point"]},
                    "x_field": {"type": "string"},
                    "y_field": {"type": "string"},
                    "title": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "description": "The data to plot, as a list of objects.",
                        "items": {"type": "object"},
                    },
                },
                "required": ["mark", "x_field", "y_field", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarifying_question",
            "description": (
                "Use when the question is ambiguous and different readings would produce "
                "materially different answers. Ends the turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decline",
            "description": (
                "Use when the question cannot or must not be answered — the data does "
                "not exist, or answering would exceed what this user is allowed to see. "
                "Ends the turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]

TERMINAL_TOOLS = {"ask_clarifying_question", "decline"}


def _run_sql(sql: str, role: str, question: str) -> str:
    if role not in ROLE_RANK:
        return f"error: unknown role {role!r}"

    if ROLE_RANK[role] < ROLE_RANK["compliance"]:
        select_list = sql.lower().split(" from ")[0]
        for col in RESTRICTED_COLUMNS:
            if col in select_list:
                return (
                    f"error: permission denied on column customers.{col} — "
                    f"role {role!r} is not cleared for it"
                )

    cache_key = question
    if cache_key in _QUERY_CACHE:
        rows = _QUERY_CACHE[cache_key]
        log.info("cache hit for %r -> %s", question, rows)
    else:
        try:
            rows = db.execute(sql)
        except db.SQLError as exc:
            return f"error: {exc}"
        _QUERY_CACHE[cache_key] = rows
        log.info("sql=%s rows=%s", sql, rows)

    if not rows:
        return "0 rows"
    header = " | ".join(rows[0].keys())
    body = "\n".join(" | ".join(str(v) for v in r.values()) for r in rows)
    return f"{len(rows)} row(s)\n{header}\n{body}"


def dispatch(name: str, args: dict, *, question: str) -> str:
    """Execute one tool call and return the result as text for the model."""
    if name == "list_tables":
        return "users, customers, transactions, alerts, case_notes"

    if name == "describe_table":
        table = args["table"]
        for stmt in db.schema_text().split("\n\n"):
            if f"TABLE {table}" in stmt:
                return stmt
        return f"error: no such table {table!r}"

    if name == "run_sql":
        return _run_sql(args["sql"], args["role"], question)

    if name == "make_chart":
        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": args.get("title", ""),
            "mark": args["mark"],
            "data": {"values": args["rows"]},
            "encoding": {
                "x": {"field": args["x_field"], "type": "nominal"},
                "y": {"field": args["y_field"], "type": "quantitative"},
            },
        }
        return f"chart rendered: {spec}"

    if name == "ask_clarifying_question":
        return args["question"]

    if name == "decline":
        return args["reason"]

    return f"error: unknown tool {name!r}"
