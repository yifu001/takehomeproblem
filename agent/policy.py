"""Server-side SQL policy engine — the enforcement point for every warehouse read.

The model never supplies identity, role, or scope: `run_query` receives the
server-resolved user and enforces the permission matrix as per-role column SETS (never
a rank ladder) over a parsed and rewritten AST. Only the rewritten, re-generated SQL is
ever executed; the model's original string is untrusted input.

Pipeline — every stage fails closed with a structured refusal:

  1. statement-kind gate   exactly one SELECT (CTEs and set operations allowed)
  2. table gate            known tables only; per-role table ACLs
  3. qualify               expand *, resolve aliases/CTEs/subqueries (sqlglot)
  4. column resolution     map every column node to its real table.column
  5. substitution          generalizable T2/T3 columns become their T1 form, but only
                           in the projection list — filters/aggregates refuse
  6. authorization         every remaining column against the role's column set
  7. row-scope rewrite     every customers reference wrapped with role predicates;
                           business tables wrapped with a forced join to the scoped
                           customers relation; users limited to the acting user's row
  8. execution             read-only connection, authorizer backstop, statement
                           timeout, row cap

Reason categories are defined once here; other modules import the constants.

Scope values (region, user_id) are bound as named SQLite parameters generated from the
rewritten AST — they originate server-side from the users table, never from model input.
"""

import sqlite3
import time

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from . import db

# ---------------------------------------------------------------- reason taxonomy

COLUMN_DENIED = "column_denied"
ROW_SCOPE = "row_scope"
TABLE_DENIED = "table_denied"
STATEMENT_KIND = "statement_kind"
PARSE = "parse"
TIMEOUT = "timeout"
FLOOR_K_ANONYMITY = "floor_k_anonymity"          # enforced by the floors stage
FLOOR_PROTECTED_CLASS = "floor_protected_class"  # enforced by the floors stage

# ---------------------------------------------------------------- permission matrix

ROW_CAP = 500
STATEMENT_TIMEOUT_SECONDS = 2.0

TIER_COLUMNS: dict[str, frozenset[str]] = {
    "T0": frozenset({"customer_id", "full_name", "region", "segment", "risk_score", "onboarded_at", "deleted_at"}),
    "T1": frozenset({"zip3", "birth_year", "income_band"}),
    "T2": frozenset({"zip_code", "dob"}),
    "T3": frozenset({"annual_income_usd"}),
    "T4": frozenset({"national_id", "email", "phone"}),
    "T5": frozenset({"race", "ethnicity", "sex"}),
}

# Per-role tier SETS on customers — deliberately not a ladder. compliance outranks
# everyone yet lacks T5; fair_lending holds T5 but is the only role that may never
# resolve an individual; admin is an operations account, not a superuser.
ROLE_CUSTOMER_TIERS: dict[str, frozenset[str]] = {
    "analyst": frozenset({"T0", "T1"}),
    "reviewer": frozenset({"T0", "T1", "T2", "T3"}),
    "compliance": frozenset({"T0", "T1", "T2", "T3", "T4"}),
    "fair_lending": frozenset({"T1", "T3", "T5"}),
    "admin": frozenset(),
}

# fair_lending's T0 is reduced to region & segment only (purpose limitation).
FAIR_LENDING_T0_COLUMNS = frozenset({"region", "segment"})

# T1 substitutes for T2/T3 in projections; T4/T5 have no coarse form and always refuse.
GENERALIZATIONS: dict[str, str] = {
    "zip_code": "zip3",
    "dob": "birth_year",
    "annual_income_usd": "income_band",
}

# Substitution (instead of denial) applies to row-grain customer-analysis roles. Other
# roles with partial sets (fair_lending) decline precise asks: their customers access is
# purpose-limited to the sanctioned aggregate path, which never emits zip3/birth_year.
GENERALIZATION_ROLES = frozenset({"analyst", "reviewer"})

REGION_SCOPED_ROLES = frozenset({"analyst", "reviewer"})
ACTIVE_ONLY_ROLES = frozenset({"analyst", "reviewer", "fair_lending"})
CASE_NOTES_ROLES = frozenset({"reviewer", "compliance"})
CUSTOMER_DATA_TABLES = frozenset({"customers", "transactions", "alerts", "case_notes"})
BUSINESS_TABLES = frozenset({"transactions", "alerts"})
KNOWN_TABLES = frozenset({"users", "customers", "transactions", "alerts", "case_notes"})

_READ_STATEMENTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_DENIED_FUNCTION_NAMES = frozenset({"load_extension", "writefile", "readfile"})

_SCHEMA_CACHE: dict[str, dict[str, str]] | None = None


# ---------------------------------------------------------------- report types


class Refusal:
    """A structured, machine-consumable denial: category plus a normalized detail."""

    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        self.detail = detail

    def __repr__(self) -> str:
        return f"Refusal({self.category}: {self.detail})"


class _PolicyRefusal(Exception):
    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.refusal = Refusal(category, detail)


def _refuse(category: str, detail: str) -> _PolicyRefusal:
    return _PolicyRefusal(category, detail)


class ExecutionReport:
    """Structured outcome of one run_query call; consumed by the audit layer."""

    def __init__(self, sql_requested: str) -> None:
        self.sql_requested = sql_requested
        self.sql_executed: str | None = None
        self.rewrites_applied: list[str] = []
        self.refusal: Refusal | None = None
        self.rows: list[dict] = []
        self.truncated: bool = False
        self.latency_ms: int | None = None


# ---------------------------------------------------------------- helpers


def allowed_customer_columns(role: str) -> frozenset[str]:
    """The role's column set on customers, unioned from its tier set (never ranks)."""
    if role == "admin":
        return frozenset()
    columns = set().union(*[TIER_COLUMNS[t] for t in ROLE_CUSTOMER_TIERS[role]])
    if role == "fair_lending":
        columns |= FAIR_LENDING_T0_COLUMNS
    return frozenset(columns)


def _qualify_schema() -> dict[str, dict[str, str]]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with db.connect() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            _SCHEMA_CACHE = {
                table: {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info('{table}')")}
                for table in tables
            }
    return _SCHEMA_CACHE


def _strip_comments(tree: exp.Expression) -> None:
    for node in tree.walk():
        node.comments = []


def _clause_position(col: exp.Column) -> str:
    """'projection' when the column sits under the innermost SELECT's projection list."""
    node: exp.Expression = col
    while node.parent is not None and not isinstance(node.parent, exp.Select):
        node = node.parent
    parent = node.parent
    if parent is None:
        return "filter"
    projections = parent.args.get("expressions") or []
    return "projection" if any(p is node for p in projections) else "filter"


def _inside_aggregate(col: exp.Column) -> bool:
    node: exp.Expression | None = col.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.AggFunc):
            return True
        node = node.parent
    return False


# ---------------------------------------------------------------- pipeline stages


def _parse_and_gate(sql: str) -> exp.Expression:
    """Reject anything that is not a single read statement before any analysis."""
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except SqlglotError:
        raise _refuse(PARSE, "statement could not be parsed")
    if len(statements) != 1:
        raise _refuse(STATEMENT_KIND, "exactly one SELECT statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, _READ_STATEMENTS):
        raise _refuse(STATEMENT_KIND, "only SELECT statements are allowed")
    if any(True for _ in stmt.find_all(exp.Placeholder)):
        raise _refuse(PARSE, "parameter placeholders are not accepted")
    for fn in stmt.find_all(exp.Func):
        if isinstance(fn, exp.Anonymous) and str(fn.name).lower() in _DENIED_FUNCTION_NAMES:
            raise _refuse(STATEMENT_KIND, "function is not permitted")
    _strip_comments(stmt)
    return stmt


def _gate_unknown_tables(tree: exp.Expression) -> None:
    """Pre-qualify name check: physical tables must be known (CTE aliases excluded)."""
    cte_aliases = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = {t.name.lower() for t in tree.find_all(exp.Table)} - cte_aliases
    if tables - KNOWN_TABLES:
        raise _refuse(TABLE_DENIED, "unknown or forbidden table")


def _gate_tables(physical_tables: list[exp.Table], role: str) -> None:
    """Role-level table ACLs over the physical tables the statement actually reads."""
    names = {t.name.lower() for t in physical_tables}
    if names - KNOWN_TABLES:
        raise _refuse(TABLE_DENIED, "unknown or forbidden table")
    if role == "admin" and names & CUSTOMER_DATA_TABLES:
        raise _refuse(TABLE_DENIED, "role 'admin' has no access to customer data")
    if "case_notes" in names and role not in CASE_NOTES_ROLES:
        raise _refuse(TABLE_DENIED, f"case_notes is not accessible to role {role!r}")


def _qualify(tree: exp.Expression) -> exp.Expression:
    try:
        return qualify(tree, schema=_qualify_schema(), dialect="sqlite")
    except SqlglotError:
        raise _refuse(PARSE, "references could not be resolved")


def _projection_source_column(select: exp.Expression, output_name: str) -> exp.Column | None:
    """The underlying column of a derived relation's output, when it is a simple column."""
    for projection in select.expressions:
        if isinstance(projection, exp.Alias):
            if (projection.alias or "").lower() == output_name.lower():
                inner = projection.this
                return inner if isinstance(inner, exp.Column) else None
        elif isinstance(projection, exp.Column):
            if projection.name.lower() == output_name.lower():
                return projection
    return None


def _physical_table_nodes(root) -> list[exp.Table]:
    """Physical Table nodes the statement reads, identified via scope sources.

    Scope-based identity (not name matching) so a CTE that shadows a table name is
    never mistaken for the physical table — or the physical table for the CTE.
    """
    nodes: list[exp.Table] = []
    seen: set[int] = set()
    for scope in root.traverse():
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and id(source) not in seen:
                seen.add(id(source))
                nodes.append(source)
    return nodes


def _resolve_columns(root_scope) -> list[tuple[exp.Column, str, str]]:
    """Resolve every column node to its real (table, column) through CTEs and subqueries.

    Returns (column_node, table, column) triples. Computed outputs (expressions) resolve
    to None internally and are skipped here — their source columns are authorized
    individually because they are column nodes of the statement themselves.
    """
    owners: dict[int, object] = {}
    for scope in root_scope.traverse():
        for col in scope.columns:
            owners[id(col)] = scope

    resolutions: dict[int, tuple[str, str] | None] = {}

    def resolve(col: exp.Column, scope) -> tuple[str, str] | None:
        if id(col) in resolutions:
            return resolutions[id(col)]
        resolutions[id(col)] = None  # cycle guard for recursive CTE self-references
        table_ref = col.table
        if not table_ref:
            raise _refuse(PARSE, "ambiguous column reference")
        source_scope = scope
        while source_scope is not None and table_ref not in source_scope.sources:
            source_scope = source_scope.parent
        if source_scope is None:
            raise _refuse(PARSE, "unresolvable column reference")
        source = source_scope.sources[table_ref]
        if isinstance(source, exp.Table):
            result: tuple[str, str] | None = (source.name.lower(), col.name.lower())
        else:  # CTE / derived table: a Scope whose expression is the source Select
            inner = _projection_source_column(source.expression, col.name)
            if inner is None:
                result = None
            else:
                owner = owners.get(id(inner))
                if owner is None:
                    raise _refuse(PARSE, "unresolvable column reference")
                result = resolve(inner, owner)
        resolutions[id(col)] = result
        return result

    resolved: list[tuple[exp.Column, str, str]] = []
    for scope in root_scope.traverse():
        for col in scope.columns:
            outcome = resolve(col, scope)
            if outcome is not None:
                resolved.append((col, outcome[0], outcome[1]))
    return resolved


def _substitute(
    resolved: list[tuple[exp.Column, str, str]], role: str, report: ExecutionReport
) -> set[int]:
    """Rewrite generalizable unauthorized columns to their T1 form, projections only."""
    allowed = allowed_customer_columns(role)
    substituted: set[int] = set()
    for col, table, column in resolved:
        if table != "customers" or column not in GENERALIZATIONS:
            continue
        generalized = GENERALIZATIONS[column]
        if role not in GENERALIZATION_ROLES or column in allowed or generalized not in allowed:
            continue
        if _clause_position(col) != "projection":
            raise _refuse(
                COLUMN_DENIED,
                f"filtering on customers.{column} is not permitted for role {role!r}; "
                "only the generalized column may be selected",
            )
        if _inside_aggregate(col):
            raise _refuse(
                COLUMN_DENIED,
                f"customers.{column} cannot be aggregated for role {role!r}; "
                "aggregate over the generalized column instead",
            )
        parent = col.parent
        if isinstance(parent, exp.Alias) and (parent.alias or "").lower() == column:
            parent.set("alias", exp.to_identifier(generalized))
        else:
            # An enclosing projection alias named after the precise column (e.g. a CAST
            # of dob) would keep the forbidden token in executed SQL — rename it too.
            node = parent
            while node is not None and not isinstance(node, exp.Select):
                if isinstance(node, exp.Alias) and (node.alias or "").lower() == column:
                    node.set("alias", exp.to_identifier(generalized))
                    break
                node = node.parent
        col.set("this", exp.to_identifier(generalized))
        substituted.add(id(col))
        report.rewrites_applied.append(f"generalize_column:{column}->{generalized}")
    return substituted


def _column_allowed(table: str, column: str, role: str) -> bool:
    if table == "customers":
        return column in allowed_customer_columns(role)
    if table == "users":
        return True  # row scope (own row) is enforced by the wrap, not columns
    if table in BUSINESS_TABLES:
        return role != "admin"  # admin is refused at the table gate regardless
    if table == "case_notes":
        return role in CASE_NOTES_ROLES
    return False


def _is_scope_join_key(col: exp.Column) -> bool:
    """True for customers.customer_id used as the key of a column-to-column join.

    Joining a business table to the scoped customers relation is the only way row scope
    can reach tables without a region column (architecture §4.4); the join key itself
    discloses no values. Literal-equality probes (customer_id = 'c012') stay refused.
    """
    parent = col.parent
    if not isinstance(parent, exp.EQ):
        return False
    other = parent.this if parent.this is not col else parent.expression
    if not isinstance(other, exp.Column):
        return False
    if col.name.lower() != "customer_id" or other.name.lower() != "customer_id":
        return False
    node: exp.Expression | None = parent
    while node is not None and not isinstance(node, (exp.Select, exp.Join)):
        node = node.parent
    return isinstance(node, exp.Join)


def _authorize(
    resolved: list[tuple[exp.Column, str, str]], role: str, substituted: set[int]
) -> None:
    for col, table, _original in resolved:
        if id(col) in substituted:
            continue  # rewritten to an authorized T1 column by construction
        current = col.name.lower()
        if not _column_allowed(table, current, role) and not _is_scope_join_key(col):
            raise _refuse(COLUMN_DENIED, f"column {table}.{current} is not permitted for role {role!r}")


def _check_anchor(physical_tables: list[exp.Table], role: str) -> None:
    """Scoped roles must anchor business-table reads with a customers reference.

    Without an anchor the user may be asking beyond their scope; silently narrowing a
    business metric to the scope would return a confident number the user did not ask
    for. Any customers reference anywhere in the statement anchors it — the rewrite
    stage guarantees every business row is scope-filtered regardless of query shape.
    """
    if role not in ACTIVE_ONLY_ROLES:
        return
    names = {t.name.lower() for t in physical_tables}
    if names & BUSINESS_TABLES and "customers" not in names:
        raise _refuse(
            ROW_SCOPE,
            f"business tables require a join to the scoped customers relation for role {role!r}",
        )


def _scoped_customers_select(role: str, region: str | None) -> tuple[exp.Expression, dict[str, str]]:
    """A customers subquery projecting exactly the role's authorized columns.

    The physical table is referenced as main.customers so a model CTE that shadows the
    table name cannot turn the wrap into a circular reference. customer_id is always in
    the projection (for non-admin roles): it is the join key the scope mechanism itself
    needs (§4.4) — references to it remain guarded by authorization, so no values escape.
    """
    projected = set(allowed_customer_columns(role))
    if role != "admin":
        projected.add("customer_id")
    columns = ", ".join(f"main.customers.{name}" for name in sorted(projected))
    predicates: list[str] = []
    params: dict[str, str] = {}
    if role in REGION_SCOPED_ROLES:
        if not region:
            raise _refuse(ROW_SCOPE, f"role {role!r} requires a region assignment")
        predicates.append("main.customers.region = :scope_region")
        params["scope_region"] = region
    if role in ACTIVE_ONLY_ROLES:
        predicates.append("main.customers.deleted_at IS NULL")
    sql = f"SELECT {columns} FROM main.customers"
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    return sqlglot.parse_one(sql, dialect="sqlite"), params


def _scoped_business_select(table: str, role: str, region: str | None) -> tuple[exp.Expression, dict[str, str]]:
    """A business-table subquery forced into a join with the scoped customers relation.

    Row semantics are preserved exactly (every business row reaches a customer through
    its foreign key); the join makes the row scope structural instead of trusting the
    model's predicates. alerts scopes through transactions; transactions directly.
    """
    scoped, params = _scoped_customers_select(role, region)
    scoped_sql = scoped.sql(dialect="sqlite")
    if table == "transactions":
        inner = (
            "SELECT _scope_t.* FROM main.transactions AS _scope_t "
            f"JOIN ({scoped_sql}) AS _scope_c ON _scope_t.customer_id = _scope_c.customer_id"
        )
    else:  # alerts
        inner = (
            "SELECT _scope_a.* FROM main.alerts AS _scope_a "
            "JOIN main.transactions AS _scope_t ON _scope_a.txn_id = _scope_t.txn_id "
            f"JOIN ({scoped_sql}) AS _scope_c ON _scope_t.customer_id = _scope_c.customer_id"
        )
    return sqlglot.parse_one(inner, dialect="sqlite"), params


def _own_row_users_select(user_id: str) -> tuple[exp.Expression, dict[str, str]]:
    sql = (
        "SELECT users.user_id, users.full_name, users.role, users.region "
        "FROM main.users WHERE users.user_id = :scope_user_id"
    )
    return sqlglot.parse_one(sql, dialect="sqlite"), {"scope_user_id": user_id}


def _wrap_row_scopes(
    physical_tables: list[exp.Table], role: str, user: dict, report: ExecutionReport
) -> dict[str, str]:
    """Replace every physical customers/users/business-table reference with a scoped one."""
    params: dict[str, str] = {}
    region = user.get("region")
    for table_node in physical_tables:
        name = table_node.name.lower()
        alias = table_node.alias_or_name
        if name == "customers":
            scoped, wrapped = _scoped_customers_select(role, region)
            params.update(wrapped)
            table_node.replace(scoped.subquery(alias=alias))
            report.rewrites_applied.append("row_scope_wrap")
        elif name == "users" and role != "admin":
            scoped, wrapped = _own_row_users_select(user["user_id"])
            params.update(wrapped)
            table_node.replace(scoped.subquery(alias=alias))
            report.rewrites_applied.append("users_own_row_scope")
        elif name in BUSINESS_TABLES and role in ACTIVE_ONLY_ROLES:
            scoped, wrapped = _scoped_business_select(name, role, region)
            params.update(wrapped)
            table_node.replace(scoped.subquery(alias=alias))
            report.rewrites_applied.append("business_row_scope_wrap")
    return params


def _regenerate(tree: exp.Expression) -> str:
    """Re-verify the rewritten tree still parses as one read statement, then emit it."""
    executed = tree.sql(dialect="sqlite")
    check = sqlglot.parse(executed, dialect="sqlite")
    if len(check) != 1 or not isinstance(check[0], _READ_STATEMENTS):
        raise _refuse(PARSE, "rewritten statement failed verification")
    return executed


# ---------------------------------------------------------------- public entry point


def run_query(sql: str, user: dict) -> ExecutionReport:
    """Authorize, rewrite, and execute one SQL statement for a server-resolved user.

    `user` is the identity from `db.get_user` — never supplied by the model. Returns an
    ExecutionReport; access failures appear as `report.refusal`, never as exceptions,
    raw SQL, or empty results masquerading as answers.
    """
    report = ExecutionReport(sql_requested=sql)
    started = time.monotonic()
    try:
        role = user["role"]
        if role not in ROLE_CUSTOMER_TIERS:
            raise _refuse(TABLE_DENIED, f"unknown role {role!r}")
        tree = _parse_and_gate(sql)
        _gate_unknown_tables(tree)
        tree = _qualify(tree)
        root_scope = build_scope(tree)
        physical_tables = _physical_table_nodes(root_scope)
        _gate_tables(physical_tables, role)
        resolved = _resolve_columns(root_scope)
        substituted = _substitute(resolved, role, report)
        _authorize(resolved, role, substituted)
        _check_anchor(physical_tables, role)
        params = _wrap_row_scopes(physical_tables, role, user, report)
        executed = _regenerate(tree)
        report.sql_executed = executed
        try:
            rows, truncated = db.execute_readonly(
                executed, params=params, timeout_seconds=STATEMENT_TIMEOUT_SECONDS, row_cap=ROW_CAP
            )
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupt" in message:
                raise _refuse(TIMEOUT, "statement exceeded the allowed execution time")
            if "not authorized" in message:
                raise _refuse(STATEMENT_KIND, "blocked by the execution authorizer")
            raise _refuse(PARSE, "query rejected at execution")
        report.rows = rows
        report.truncated = truncated
    except _PolicyRefusal as refusal:
        report.refusal = refusal.refusal
    report.rewrites_applied = list(dict.fromkeys(report.rewrites_applied))
    report.latency_ms = int((time.monotonic() - started) * 1000)
    return report
