"""Tool definitions and dispatch for the agent.

This is the layer between the model and the data. Every data-bearing tool result is
produced by the policy engine (agent/policy.py) or by role-scoped introspection here —
the model never touches the database directly and never supplies identity: the acting
user is resolved server-side per turn and handed to `dispatch` / `run_sql_tool`.

Error contract: tool failures are normalized to `error: query refused by access policy
(<reason category>)` — never SQL, never data, never raw sqlite messages.
"""

import json
import logging
import secrets

from . import db, policy

log = logging.getLogger("agent.tools")

# Result sets the policy engine has authorized, keyed by an unguessable handle and
# bound to the identity that created them. make_chart accepts nothing else: hand-written
# rows cannot reach a chart because the schema has no rows parameter and dispatch
# charts only policy-issued, identity-owned handles.
_RESULT_SETS: dict[str, dict] = {}

TERMINAL_TOOLS = {"ask_clarifying_question", "decline"}

REFUSED = "error: query refused by access policy"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "List the warehouse tables your identity is allowed to read.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "Return the column names and types of one table your identity is "
                "allowed to read. Names and types only — never data values."
            ),
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
                "Run ONE read-only SELECT statement against the warehouse; the matching "
                "rows come back with a result handle you can pass to make_chart. Never "
                "supply a role or identity — they are resolved server-side on every "
                "turn and any role argument is ignored. Do not hand-write scope "
                "predicates (region filters or deleted_at IS NULL): row scoping is "
                "applied automatically, and for some roles those columns sit outside "
                "the allowed set, so the statement is refused. For fair_lending, "
                "statements over customers must use the sanctioned aggregate shape: "
                "SELECT <keys>, COUNT(*) FROM customers GROUP BY <keys> with keys from "
                "region, segment, income_band, race, ethnicity or sex, and filters only "
                "on region, segment or income_band. transactions and alerts carry no "
                "region: to answer region-scoped questions about them, join them to "
                "customers on customer_id — a statement over those tables with no "
                "customers join is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A single SELECT statement."},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_chart",
            "description": (
                "Render a Vega-Lite chart from an authorized result set. Pass the "
                "handle returned by run_sql — never raw rows; hand-written data is "
                "refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "The handle run_sql returned."},
                    "mark": {"type": "string", "enum": ["bar", "line", "point"]},
                    "x_field": {"type": "string"},
                    "y_field": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["handle", "mark", "x_field", "y_field"],
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


def strip_role_claims(args: dict) -> dict:
    """Drop identity claims the model smuggled into tool arguments.

    The tool schemas have no role parameter and the server resolves identity per turn;
    a claim (e.g. dictated by injected text in tool output) is ignored — and, because
    this runs before anything is recorded, it never reaches the transcript either.
    """
    return {key: value for key, value in args.items() if key != "role"}


def _refusal_text(category: str) -> str:
    return f"{REFUSED} ({category})"


def _normalized_parse_refusal(sql_requested: str) -> policy.ExecutionReport:
    report = policy.ExecutionReport(sql_requested=sql_requested)
    report.refusal = policy.Refusal(policy.PARSE, "statement could not be processed")
    return report


def run_sql_tool(sql: str, user: dict) -> tuple[str, policy.ExecutionReport]:
    """Run one statement through the policy engine for a server-resolved identity.

    Returns the model-facing text and the ExecutionReport. Refusals serialize as the
    normalized category-only message; successful results serialize the rows, the
    floors' suppression notes (never silent), and a chart handle bound to this identity.
    """
    if not isinstance(sql, str) or not sql.strip():
        report = _normalized_parse_refusal("")
        return _refusal_text(report.refusal.category), report
    try:
        report = policy.run_query(sql, user)
    except Exception:  # fail closed: even an internal error is a normalized refusal
        log.exception("run_sql internal failure — refusing")
        report = _normalized_parse_refusal(sql)
        return _refusal_text(report.refusal.category), report

    if report.refusal is not None:
        log.info("run_sql refused (%s)", report.refusal.category)
        return _refusal_text(report.refusal.category), report

    handle = "r-" + secrets.token_hex(8)
    _RESULT_SETS[handle] = {"user_id": user["user_id"], "rows": report.rows}
    lines = [f"{len(report.rows)} row(s)", f"handle: {handle}"]
    if report.rows:
        lines.append(" | ".join(report.rows[0].keys()))
        lines.extend(" | ".join(str(value) for value in row.values()) for row in report.rows)
    lines.extend(report.notes)
    if report.truncated:
        lines.append("note: results were truncated at the row cap")
    return "\n".join(lines), report


def _readable_tables(role: str) -> list[str]:
    """Tables the role may read anything from — introspection respects the same ACLs."""
    if role == "admin":
        return ["users"]
    tables = ["customers", "transactions", "alerts", "users"]
    if role in policy.CASE_NOTES_ROLES:
        tables.append("case_notes")
    return sorted(tables)


def _list_tables(user: dict) -> str:
    return ", ".join(_readable_tables(user["role"]))


def _describe_table(table: str, user: dict) -> str:
    """Column names and types only — never a sample value, for any role."""
    if table not in _readable_tables(user["role"]):
        return _refusal_text(policy.TABLE_DENIED)
    with db.connect() as conn:
        info = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    lines = [f"{table} columns (name, type):"]
    lines.extend(f"  {row[1]} {row[2]}" for row in info)
    return "\n".join(lines)


def _make_chart(args: dict, user: dict) -> str:
    """Chart one policy-issued result handle. Rows never come from the model."""
    handle = args.get("handle")
    entry = _RESULT_SETS.get(handle) if isinstance(handle, str) else None
    if entry is None or entry["user_id"] != user["user_id"]:
        return "error: chart refused by access policy (no authorized result set for this handle)"
    mark = args.get("mark")
    x_field = args.get("x_field")
    y_field = args.get("y_field")
    if mark not in ("bar", "line", "point") or not x_field or not y_field:
        return "error: chart refused (mark, x_field and y_field are required)"
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": args.get("title", ""),
        "mark": mark,
        "data": {"values": entry["rows"]},
        "encoding": {
            "x": {"field": x_field, "type": "nominal"},
            "y": {"field": y_field, "type": "quantitative"},
        },
    }
    return f"chart rendered: {json.dumps(spec)}"


def dispatch(name: str, args: dict, *, user: dict) -> str:
    """Execute one tool call and return the result as text for the model.

    `user` is the server-resolved identity (db.get_user) for this turn — never taken
    from model input. Every data-bearing path runs through the policy engine.
    """
    if name == "run_sql":
        text, _report = run_sql_tool(args.get("sql", ""), user)
        return text
    if name == "list_tables":
        return _list_tables(user)
    if name == "describe_table":
        return _describe_table(str(args.get("table", "")), user)
    if name == "make_chart":
        return _make_chart(args, user)
    if name == "ask_clarifying_question":
        return str(args.get("question", ""))
    if name == "decline":
        return str(args.get("reason", ""))
    return f"error: unknown tool {name!r}"


def clear_result_sets() -> None:
    """Drop all policy-issued chart handles (between eval runs)."""
    _RESULT_SETS.clear()
