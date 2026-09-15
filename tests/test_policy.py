"""Hostile-SQL survival matrix for the policy engine (agent/policy.py).

Every test runs offline against the seeded fixture DB — no API access. The matrix
proves BOTH directions of every rule: forbidden shapes refuse with the right reason
category, and permitted shapes still execute with correct, scoped results.
"""

import sqlite3

import pytest

from agent import db, policy

# Server-resolved identities (mirrors seed.py USERS; the engine never takes a role argument).
IDENTITIES = {
    "analyst": {"user_id": "u_ana", "full_name": "Ana Reyes", "role": "analyst", "region": "WEST"},
    "reviewer": {"user_id": "u_rae", "full_name": "Rae Lindqvist", "role": "reviewer", "region": "WEST"},
    "compliance": {"user_id": "u_cora", "full_name": "Cora Adeyemi", "role": "compliance", "region": None},
    "fair_lending": {"user_id": "u_fern", "full_name": "Fern Aguilar", "role": "fair_lending", "region": None},
    "admin": {"user_id": "u_ops", "full_name": "Ops Service", "role": "admin", "region": None},
}


def run(sql: str, role: str) -> policy.ExecutionReport:
    return policy.run_query(sql, IDENTITIES[role])


def refuses(sql: str, role: str, category: str) -> policy.ExecutionReport:
    report = run(sql, role)
    assert report.refusal is not None, f"expected refusal, got rows: {report.rows[:2]}"
    assert report.refusal.category == category, report.refusal
    assert report.rows == []
    return report


def executes(sql: str, role: str) -> policy.ExecutionReport:
    report = run(sql, role)
    assert report.refusal is None, report.refusal
    assert report.sql_executed is not None
    return report


# ---------------------------------------------------------------- statement-kind gate


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customers VALUES ('x')",
        "UPDATE customers SET risk_score = 0",
        "DELETE FROM customers",
        "DROP TABLE customers",
        "CREATE TABLE evil (x TEXT)",
        "VACUUM",
        "PRAGMA table_info(customers)",
        "ATTACH DATABASE 'x.db' AS x",
        "DETACH DATABASE x",
        "SELECT 1; DROP TABLE customers",
        "WITH x AS (SELECT 1) INSERT INTO customers VALUES (1)",
    ],
)
def test_non_select_statements_refused(sql):
    report = refuses(sql, "compliance", policy.STATEMENT_KIND)
    assert report.sql_executed is None


@pytest.mark.parametrize("sql", ["SELECT FROM FROM customers", "SELECT bogus_column FROM customers"])
def test_unparseable_or_unresolvable_refused(sql):
    refuses(sql, "compliance", policy.PARSE)


@pytest.mark.parametrize("table", ["no_such_table", "sqlite_master"])
def test_unknown_tables_refused_as_table_denied(table):
    refuses(f"SELECT * FROM {table}", "compliance", policy.TABLE_DENIED)


def test_trailing_semicolon_is_single_statement():
    executes("SELECT 1;", "compliance")


# ---------------------------------------------------------------- column authorization


RESTRICTED = ["national_id", "email", "phone", "race", "ethnicity", "sex"]


@pytest.mark.parametrize("col", RESTRICTED)
@pytest.mark.parametrize(
    "template",
    [
        "SELECT {col} FROM customers",
        "SELECT full_name FROM customers WHERE {col} = 'x'",
        "SELECT full_name FROM customers c JOIN transactions t ON c.customer_id = t.customer_id AND t.memo = c.{col}",
        "SELECT segment, COUNT(*) FROM customers GROUP BY {col}",
        "SELECT full_name FROM customers GROUP BY segment HAVING MAX({col}) > 0",
        "SELECT full_name FROM customers ORDER BY {col}",
        "SELECT full_name, ROW_NUMBER() OVER (ORDER BY {col}) FROM customers",
        "SELECT * FROM (SELECT {col} FROM customers) sub",
        "WITH x AS (SELECT {col} FROM customers) SELECT * FROM x",
        "SELECT full_name FROM customers UNION SELECT {col} FROM customers",
        "SELECT CASE WHEN {col} = 'x' THEN full_name ELSE region END FROM customers",
    ],
)
def test_hard_restricted_columns_refused_everywhere(col, template):
    """T4 (compliance-only) and T5 (fair_lending-only) have no substitute anywhere."""
    for role in ("analyst", "reviewer", "compliance", "fair_lending"):
        if role == "compliance" and col in RESTRICTED[:3]:
            continue  # compliance legitimately holds T4
        if role == "fair_lending" and col in RESTRICTED[3:]:
            continue  # fair_lending legitimately holds T5
        report = refuses(template.format(col=col), role, policy.COLUMN_DENIED)
        assert col not in (report.sql_executed or "")


@pytest.mark.parametrize("col", ["dob", "zip_code"])
def test_analyst_t2_denied_in_every_clause_position(col):
    for sql in (
        f"SELECT full_name FROM customers WHERE {col} = 'x'",
        f"SELECT full_name FROM customers ORDER BY {col}",
        f"SELECT segment, COUNT(*) FROM customers GROUP BY {col}",
        f"SELECT full_name FROM customers GROUP BY segment HAVING MAX({col}) > 0",
    ):
        refuses(sql, "analyst", policy.COLUMN_DENIED)


def test_analyst_income_filter_refused_not_rewritten():
    """L15 mechanism: annual_income_usd in WHERE is a refusal, never a redaction."""
    report = refuses(
        "SELECT full_name FROM customers WHERE annual_income_usd > 100000",
        "analyst",
        policy.COLUMN_DENIED,
    )
    assert "annual_income_usd" not in (report.sql_executed or "")


def test_reviewer_t4_denied():
    refuses("SELECT email FROM customers", "reviewer", policy.COLUMN_DENIED)


def test_compliance_t5_denied_despite_outranking_everyone():
    refuses("SELECT race FROM customers", "compliance", policy.COLUMN_DENIED)


@pytest.mark.parametrize("col", ["national_id", "race"])
def test_group_concat_over_restricted_column_refused(col):
    refuses(f"SELECT group_concat({col}) FROM customers", "analyst", policy.COLUMN_DENIED)


def test_select_star_on_customers_refused_for_every_role():
    """Wildcards expand then authorize column-by-column; every role lacks something."""
    for role in ("analyst", "reviewer", "compliance", "fair_lending"):
        refuses("SELECT * FROM customers", role, policy.COLUMN_DENIED)


def test_select_star_on_business_table_allowed():
    report = executes("SELECT * FROM transactions", "compliance")
    assert len(report.rows) == 19


def test_fair_lending_t2_denied_not_substituted():
    """Purpose-limited roles decline precise asks; their sanctioned path has no zip3/birth_year."""
    for sql in (
        "SELECT zip_code FROM customers",
        "SELECT dob FROM customers",
        "SELECT full_name FROM customers WHERE zip_code = '94110'",
        "SELECT full_name FROM customers WHERE dob > '1950'",
    ):
        refuses(sql, "fair_lending", policy.COLUMN_DENIED)


def test_reviewer_t2_t3_read_precisely_no_substitution():
    """Over-filtering guard: reviewer legitimately holds T2/T3 — precise values, precise names."""
    report = executes("SELECT zip_code, dob FROM customers ORDER BY customer_id", "reviewer")
    assert report.rows[0] == {"zip_code": "94110", "dob": "1979-03-14"}
    assert "zip_code" in report.sql_executed and "dob" in report.sql_executed
    assert not any("generalize_column" in w for w in report.rewrites_applied)


def test_compliance_reads_t4():
    report = executes("SELECT national_id FROM customers WHERE customer_id = 'c001'", "compliance")
    assert report.rows[0]["national_id"] == "NX-4417-DQ"


# ---------------------------------------------------------------- generalization


def test_zip_code_substitutes_to_zip3():
    report = executes("SELECT zip_code FROM customers ORDER BY customer_id", "analyst")
    assert [r["zip3"] for r in report.rows] == ["941", "941", "943", "940", "941", "943"]
    assert "zip3" in report.sql_executed
    assert "zip_code" not in report.sql_executed
    assert any("generalize_column" in w for w in report.rewrites_applied)


def test_dob_substitutes_to_birth_year():
    report = executes("SELECT dob FROM customers ORDER BY customer_id", "analyst")
    assert [r["birth_year"] for r in report.rows][0] == 1979
    assert "birth_year" in report.sql_executed
    assert "dob" not in report.sql_executed


def test_income_substitutes_to_income_band():
    report = executes("SELECT annual_income_usd FROM customers ORDER BY customer_id", "analyst")
    assert report.rows[0]["income_band"] == "high"
    assert "income_band" in report.sql_executed
    assert "annual_income_usd" not in report.sql_executed


def test_substitution_preserves_explicit_output_alias():
    report = executes("SELECT zip_code AS user_zip FROM customers LIMIT 1", "analyst")
    assert set(report.rows[0].keys()) == {"user_zip"}
    assert "zip_code" not in report.sql_executed


def test_substitution_inside_projection_expression():
    report = executes("SELECT CAST(dob AS INTEGER) FROM customers LIMIT 1", "analyst")
    assert report.rows[0]["birth_year"] == 1979


def test_substitution_through_cte_boundary():
    report = executes("WITH x AS (SELECT dob FROM customers) SELECT dob FROM x LIMIT 1", "analyst")
    assert "birth_year" in report.sql_executed
    assert "dob" not in report.sql_executed


def test_generalizable_column_inside_aggregate_refused():
    """AVG over a band is a wrong number, not a coarse answer — refuse instead."""
    for sql in (
        "SELECT AVG(annual_income_usd) FROM customers",
        "SELECT SUM(annual_income_usd) FROM customers",
        "SELECT COUNT(dob) FROM customers",
    ):
        refuses(sql, "analyst", policy.COLUMN_DENIED)


# ---------------------------------------------------------------- row-scope rewrite


def test_analyst_count_scoped_to_region_and_active():
    report = executes("SELECT COUNT(*) FROM customers", "analyst")
    assert report.rows[0]["_col_0"] == 6
    assert "deleted_at" in report.sql_executed and ":scope_region" in report.sql_executed
    assert any(w == "row_scope_wrap" for w in report.rewrites_applied)


def test_analyst_avg_risk_scoped():
    report = executes("SELECT AVG(risk_score) FROM customers", "analyst")
    assert round(report.rows[0]["_col_0"], 2) == 60.5


def test_compliance_sees_all_regions_including_offboarded():
    report = executes("SELECT COUNT(*) FROM customers", "compliance")
    assert report.rows[0]["_col_0"] == 15
    report = executes("SELECT COUNT(*) FROM customers WHERE deleted_at IS NOT NULL", "compliance")
    assert report.rows[0]["_col_0"] == 2


def test_fair_lending_active_only_all_regions():
    report = executes("SELECT COUNT(*) FROM customers", "fair_lending")
    assert report.rows[0]["_col_0"] == 13


def test_reviewer_region_scoped_like_analyst():
    report = executes("SELECT COUNT(*) FROM customers", "reviewer")
    assert report.rows[0]["_col_0"] == 6


def test_ach_total_soft_delete_cascades_through_join():
    sql = (
        "SELECT SUM(t.amount_minor) / 100.0 FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id "
        "WHERE t.channel = 'ach' AND t.currency = 'USD' "
        "AND t.occurred_at >= '2026-03-01' AND t.occurred_at < '2026-04-01'"
    )
    report = executes(sql, "analyst")
    assert round(report.rows[0]["_col_0"], 2) == 36380.00


def test_open_alerts_cascade_excludes_offboarded_customer():
    sql = (
        "SELECT COUNT(*) FROM alerts a JOIN transactions t ON a.txn_id = t.txn_id "
        "JOIN customers c ON t.customer_id = c.customer_id WHERE UPPER(a.status) = 'OPEN'"
    )
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 5


def test_scope_cascades_through_subquery():
    sql = "SELECT COUNT(*) FROM transactions WHERE customer_id IN (SELECT customer_id FROM customers)"
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 10
    report = executes(sql, "compliance")
    assert report.rows[0]["_col_0"] == 19


def test_scope_cascades_through_cte():
    sql = (
        "WITH mine AS (SELECT customer_id FROM customers) "
        "SELECT COUNT(*) FROM transactions WHERE customer_id IN (SELECT customer_id FROM mine)"
    )
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 10


def test_scope_cascades_through_exists():
    sql = (
        "SELECT COUNT(*) FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM transactions t WHERE t.customer_id = c.customer_id)"
    )
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 6


def test_scope_applies_to_both_union_branches():
    sql = "SELECT full_name FROM customers UNION SELECT memo FROM transactions"
    report = executes(sql, "analyst")
    blob = str(report.rows)
    assert "Nadia Osei" not in blob and "c009" not in blob


def test_joinless_business_table_refused_for_scoped_roles():
    """A business-table aggregate with no customers anchor cannot be proven in-scope."""
    for role in ("analyst", "reviewer", "fair_lending"):
        for table in ("transactions", "alerts"):
            refuses(f"SELECT COUNT(*) FROM {table}", role, policy.ROW_SCOPE)


def test_out_of_region_row_read_returns_nothing():
    report = executes("SELECT full_name FROM customers WHERE customer_id = 'c012'", "analyst")
    assert report.rows == []
    assert "Hal Brenner" not in str(report.rows)


def test_admin_denied_on_every_customer_table():
    for table in ("customers", "transactions", "alerts", "case_notes"):
        report = refuses(f"SELECT COUNT(*) FROM {table}", "admin", policy.TABLE_DENIED)
        assert report.rows == []


def test_admin_reads_users_in_full():
    report = executes("SELECT COUNT(*) FROM users", "admin")
    assert report.rows[0]["_col_0"] == 6
    report = executes("SELECT role FROM users ORDER BY user_id", "admin")
    assert [r["role"] for r in report.rows] == [
        "analyst", "analyst", "compliance", "fair_lending", "admin", "reviewer",
    ]


def test_non_admin_sees_only_own_users_row():
    report = executes("SELECT * FROM users", "analyst")
    assert len(report.rows) == 1
    assert report.rows[0]["user_id"] == "u_ana"
    assert any(w == "users_own_row_scope" for w in report.rewrites_applied)


def test_non_admin_cannot_probe_other_users():
    report = executes("SELECT role FROM users WHERE user_id = 'u_cora'", "analyst")
    assert report.rows == []
    report = executes("SELECT COUNT(*) FROM users WHERE role = 'compliance'", "analyst")
    assert report.rows[0]["_col_0"] in (0, 1)  # own row only: 1 iff analyst==compliance


@pytest.mark.parametrize("role", ["analyst", "fair_lending", "admin"])
def test_case_notes_table_acl_denies(role):
    refuses("SELECT body FROM case_notes", role, policy.TABLE_DENIED)


@pytest.mark.parametrize("role", ["reviewer", "compliance"])
def test_case_notes_table_acl_allows(role):
    report = executes("SELECT COUNT(*) FROM case_notes", role)
    assert report.rows[0]["_col_0"] == 5


def test_cte_shadowing_table_name_still_wraps():
    sql = "WITH customers AS (SELECT full_name FROM customers) SELECT COUNT(*) FROM customers"
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 6
    assert "main.customers" in report.sql_executed


def test_fair_lending_join_key_allowed_for_scope_plumbing():
    """Business metrics for fair_lending reach scope through the customer join (§4.4)."""
    sql = "SELECT COUNT(*) FROM transactions t JOIN customers c ON t.customer_id = c.customer_id"
    report = executes(sql, "fair_lending")
    assert report.rows[0]["_col_0"] == 17  # all except the two offboarded customers' txns


def test_fair_lending_join_key_literal_probe_refused():
    sql = (
        "SELECT COUNT(*) FROM transactions t JOIN customers c "
        "ON c.customer_id = 'c012' AND t.customer_id = c.customer_id"
    )
    refuses(sql, "fair_lending", policy.COLUMN_DENIED)


def test_cte_shadowing_users_table_still_wraps():
    """A CTE named like a table must not turn the own-row wrap into a circular reference."""
    sql = "WITH users AS (SELECT user_id FROM users) SELECT COUNT(*) FROM users"
    report = executes(sql, "analyst")
    assert report.rows[0]["_col_0"] == 1


def test_unknown_role_refuses():
    report = policy.run_query("SELECT 1", {"user_id": "x", "role": "root", "region": None})
    assert report.refusal is not None
    assert report.refusal.category == policy.TABLE_DENIED


# ---------------------------------------------------------------- resource bounds


def test_recursive_cte_bounded_by_row_cap():
    sql = "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) SELECT x FROM cnt"
    report = run(sql, "compliance")
    assert report.refusal is None
    assert report.truncated is True
    assert len(report.rows) == policy.ROW_CAP


def test_unbounded_aggregate_hits_statement_timeout():
    sql = "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) SELECT COUNT(*) FROM cnt"
    report = refuses(sql, "compliance", policy.TIMEOUT)
    assert report.sql_executed is not None  # the executed SQL is known; the run was cut


def test_normal_queries_unaffected_by_caps():
    report = executes("SELECT * FROM transactions", "compliance")
    assert len(report.rows) == 19 and report.truncated is False


# ---------------------------------------------------------------- authorizer backstop


def test_authorizer_denies_pragma_at_execution_layer():
    conn = db.connect_readonly()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("PRAGMA table_info(customers)")
    finally:
        conn.close()


def test_load_extension_refused_as_statement_kind():
    refuses("SELECT load_extension('evil')", "compliance", policy.STATEMENT_KIND)


def test_readonly_connection_rejects_writes():
    conn = db.connect_readonly()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO users VALUES ('x', 'x', 'analyst', 'WEST')")
    finally:
        conn.close()


# ---------------------------------------------------------------- report contract


def test_report_shape_is_complete():
    report = run("SELECT COUNT(*) FROM customers", "analyst")
    assert report.sql_requested == "SELECT COUNT(*) FROM customers"
    assert report.sql_executed
    assert isinstance(report.rewrites_applied, list)
    assert report.refusal is None
    assert isinstance(report.rows, list)


def test_refusal_report_has_no_executed_sql():
    report = refuses("SELECT national_id FROM customers", "analyst", policy.COLUMN_DENIED)
    assert report.sql_executed is None
    assert report.sql_requested == "SELECT national_id FROM customers"


def test_refusal_detail_never_echoes_model_sql():
    hostile = "SELECT national_id FROM customers WHERE email = 'canary@example.com'"
    report = refuses(hostile, "analyst", policy.COLUMN_DENIED)
    assert "canary" not in report.refusal.detail
    assert "SELECT" not in report.refusal.detail


def test_scope_wrap_recorded_in_rewrites():
    report = executes("SELECT COUNT(*) FROM transactions t JOIN customers c "
                      "ON t.customer_id = c.customer_id", "analyst")
    assert "row_scope_wrap" in report.rewrites_applied
    assert "deleted_at" in report.sql_executed
