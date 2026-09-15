"""Database access for the agent.

`connect` / `execute` / `schema_text` serve the legacy baseline paths. The policy engine
runs every agent-facing query through `connect_readonly` / `execute_readonly`, which
hard-enforce read-only access at the SQLite layer: mode=ro URI, PRAGMA query_only, an
authorizer denying non-read actions and a function denylist, a progress-handler statement
timeout, and a hard row cap.
"""

import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fraud.db")

ROW_CAP = 500
STATEMENT_TIMEOUT_SECONDS = 2.0

# Function names the authorizer refuses even if a crafted statement reaches execution.
_DENIED_FUNCTIONS = frozenset({"load_extension", "writefile", "readfile"})

# Authorizer actions that must never succeed on an agent-facing connection. Reads
# (SQLITE_READ/SQLITE_SELECT/SQLITE_FUNCTION-outside-denylist) stay allowed; the policy
# engine's parse gate, not the authorizer, is what keeps statements SELECT-only.
_DENIED_ACTIONS = frozenset(
    code
    for code in (
        getattr(sqlite3, name, None)
        for name in (
            "SQLITE_PRAGMA", "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_COPY",
            "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_ALTER_TABLE",
            "SQLITE_CREATE_TABLE", "SQLITE_DROP_TABLE", "SQLITE_CREATE_INDEX",
            "SQLITE_DROP_INDEX", "SQLITE_CREATE_VIEW", "SQLITE_DROP_VIEW",
            "SQLITE_CREATE_TRIGGER", "SQLITE_DROP_TRIGGER", "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
        )
    )
    if code is not None
)


class SQLError(Exception):
    """A query failed. The message is returned to the model verbatim."""


def _authorizer(action: int, arg1, arg2, db_name, trigger_or_view) -> int:
    """Backstop for the policy engine: deny every non-read action and denied functions."""
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg1 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"{DB_PATH} not found — run `python seed.py` first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly() -> sqlite3.Connection:
    """An agent-facing connection: read-only file mode, query_only, authorizer backstop."""
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"{DB_PATH} not found — run `python seed.py` first.")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")  # before the authorizer, which denies all pragmas
    conn.set_authorizer(_authorizer)
    return conn


def execute_readonly(
    sql: str,
    *,
    params: dict | None = None,
    timeout_seconds: float = STATEMENT_TIMEOUT_SECONDS,
    row_cap: int = ROW_CAP,
) -> tuple[list[dict], bool]:
    """Run one (already policy-rewritten) statement with a timeout and row cap.

    Returns (rows, truncated). The statement timeout is enforced by a progress handler;
    a query that would exceed the row cap is cut short and `truncated` is True.
    Raises sqlite3.OperationalError on timeout/interruption or authorizer denial.
    """
    conn = connect_readonly()
    try:
        deadline = time.monotonic() + timeout_seconds

        def _interrupt() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_interrupt, 1000)
        cursor = conn.execute(sql, params or {})
        rows: list[dict] = []
        truncated = False
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            rows.extend(dict(r) for r in batch)
            if len(rows) > row_cap:
                rows = rows[:row_cap]
                truncated = True
                break
        return rows, truncated
    finally:
        conn.close()


def get_user(user_id: str) -> dict:
    """The authenticated identity. This is the only trustworthy source of role and region."""
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, full_name, role, region FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError(f"no such user: {user_id}")
    return dict(row)


def execute(sql: str) -> list[dict]:
    """Run `sql` and return rows as dicts.

    No statement timeout, no row cap, no read-only connection, no statement-kind check.
    """
    with connect() as conn:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise SQLError(str(exc)) from exc
    return [dict(r) for r in rows]


def schema_text() -> str:
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return "\n\n".join(r["sql"] for r in rows if r["sql"])
