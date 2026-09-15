"""Database access for the baseline agent."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fraud.db")


class SQLError(Exception):
    """A query failed. The message is returned to the model verbatim."""


def connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"{DB_PATH} not found — run `python seed.py` first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
