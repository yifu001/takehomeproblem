"""Floor tests: the k=2 customer-aggregate floor and the protected-class reporting rule.

Every rule is proven in BOTH directions — refusals with the right category, and the
over-filtering traps (row-grain reads, business metrics, floor-edge successes) still
passing. All cases run offline against the seeded fixture DB.
"""

import re

import pytest

from agent import floors, policy

IDENTITIES = {
    "analyst": {"user_id": "u_ana", "full_name": "Ana Reyes", "role": "analyst", "region": "WEST"},
    "reviewer": {"user_id": "u_rae", "full_name": "Rae Lindqvist", "role": "reviewer", "region": "WEST"},
    "compliance": {"user_id": "u_cora", "full_name": "Cora Adeyemi", "role": "compliance", "region": None},
    "fair_lending": {"user_id": "u_fern", "full_name": "Fern Aguilar", "role": "fair_lending", "region": None},
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


# ---------------------------------------------------------------- k=2 customer-aggregate floor


def test_single_customer_aggregate_refuses_with_floor_k_anonymity():
    """L3: an aggregate resolving to one customer cites the floor, never '0 rows'."""
    report = refuses(
        "SELECT COUNT(*) FROM customers WHERE risk_score > 90", "analyst", policy.FLOOR_K_ANONYMITY
    )
    # The report records the rewritten statement the floors stopped — the scope wrap,
    # never the model's original string. Nothing was executed (refuses asserts no rows).
    assert report.sql_executed is not None
    assert "deleted_at" in report.sql_executed
    assert "risk_score > 90" not in report.sql_executed
    assert "1 distinct customer" in report.refusal.detail
    assert "0 rows" not in report.refusal.detail


def test_exactly_two_customers_aggregate_succeeds():
    """VAL-CORR-023: the floor fires only below 2 — k=2 must not over-refuse."""
    report = executes(
        "SELECT COUNT(*) FROM customers WHERE deleted_at IS NOT NULL", "compliance"
    )
    assert report.rows[0]["_col_0"] == 2  # c004 and c015, exactly 2 distinct customers


def test_row_grain_single_customer_succeeds():
    """VAL-CORR-024 mechanism: row-grain reads are not k-floored."""
    report = executes(
        "SELECT full_name, risk_score FROM customers WHERE customer_id = 'c001'", "analyst"
    )
    assert len(report.rows) == 1
    assert report.rows[0]["full_name"] == "Dana Whitfield"


def test_business_metric_average_with_customer_join_succeeds():
    """VAL-CORR-025: transaction metrics are exempt even when joined to customers."""
    report = executes(
        "SELECT AVG(t.amount_minor) / 100.0 FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id",
        "analyst",
    )
    assert round(report.rows[0]["_col_0"], 2) == 10991.55  # 10 in-scope transactions


def test_group_by_sub_floor_group_dropped_with_note():
    """VAL-CORR-026: a group of 1 is dropped WITH a note, never silently."""
    report = executes(
        "SELECT region, COUNT(*) FROM customers WHERE risk_score > 60 GROUP BY region",
        "compliance",
    )
    # WEST: c001/c003/c006 = 3, EAST: c008/c009/c012 = 3, CENTRAL: c015 alone = 1 -> dropped
    assert {row["region"]: row["_col_1"] for row in report.rows} == {"EAST": 3, "WEST": 3}
    assert "CENTRAL" not in str(report.rows)
    assert report.notes == [floors.K_SUPPRESSION_NOTE]
    assert "group_k_floor" in report.rewrites_applied
    assert "COUNT(DISTINCT" in report.sql_executed and "HAVING" in report.sql_executed


def test_group_by_all_groups_sub_floor_refuses():
    report = refuses(
        "SELECT risk_score, COUNT(*) FROM customers GROUP BY risk_score",
        "analyst",
        policy.FLOOR_K_ANONYMITY,
    )  # every WEST active risk score is unique: every group is a single customer


def test_grouped_exactly_two_per_group_succeeds_without_rewrite():
    report = executes(
        "SELECT income_band, COUNT(*) FROM customers GROUP BY income_band", "analyst"
    )
    # WEST active: high {c001, c007}, middle {c002, c006}, upper-middle {c003, c005} — k=2 each
    assert {row["income_band"]: row["_col_1"] for row in report.rows} == {
        "high": 2,
        "middle": 2,
        "upper-middle": 2,
    }
    assert report.notes == []
    assert "COUNT(DISTINCT" not in report.sql_executed  # no suppression needed, no rewrite


def test_suppressed_group_value_never_disclosed():
    """VAL-LEAK-032: the note carries no sizes or names, and the withheld value is absent.

    WEST active with risk_score > 40: retail {91, 42, 58, 71} (k=4) and business {66}
    (k=1). The business average IS one customer's exact risk score — it must not appear.
    """
    report = executes(
        "SELECT segment, AVG(risk_score) FROM customers WHERE risk_score > 40 GROUP BY segment",
        "analyst",
    )
    assert report.rows == [{"segment": "retail", "_col_1": 65.5}]
    assert report.notes == [floors.K_SUPPRESSION_NOTE]
    assert "business" not in str(report.rows)
    assert all(row["_col_1"] != 66 for row in report.rows)
    assert re.findall(r"\d+", floors.K_SUPPRESSION_NOTE) == ["2"]  # the policy threshold only


def test_note_defaults_to_empty_for_unfloored_results():
    report = executes("SELECT COUNT(*) FROM customers", "analyst")
    assert report.rows[0]["_col_0"] == 6
    assert report.notes == []


# ---------------------------------------------------------------- sanctioned path (fair_lending)


def test_c8_sex_breakdown_reports_at_the_floor_edge():
    """VAL-CORR-007: population 13, cells 7/6 — the sanctioned path must report."""
    report = executes("SELECT sex, COUNT(*) FROM customers GROUP BY sex", "fair_lending")
    assert report.rows == [{"sex": "F", "_col_1": 7}, {"sex": "M", "_col_1": 6}]
    assert "sanctioned_aggregate_path" in report.rewrites_applied
    # Server-constructed aggregate: COUNT(DISTINCT customer_id), scoped, active-only.
    assert "COUNT(DISTINCT" in report.sql_executed
    assert "deleted_at IS NULL" in report.sql_executed


def test_l14_population_below_ten_refuses_whole_breakdown():
    report = refuses(
        "SELECT race, COUNT(*) FROM customers WHERE region = 'WEST' GROUP BY race",
        "fair_lending",
        policy.FLOOR_PROTECTED_CLASS,
    )
    assert "10" in report.refusal.detail


def test_l17_single_suppressed_cell_derivable_refuses_breakdown():
    """Ethnicity: cells 11/2 — suppressing the 2 would leave it derivable from the total."""
    report = refuses(
        "SELECT ethnicity, COUNT(*) FROM customers GROUP BY ethnicity",
        "fair_lending",
        policy.FLOOR_PROTECTED_CLASS,
    )
    assert "derivable" in report.refusal.detail


def test_race_breakdown_suppresses_two_cells_with_note():
    """Active races 6/4/2/1: the two sub-floor cells are suppressed together (safe)."""
    report = executes("SELECT race, COUNT(*) FROM customers GROUP BY race", "fair_lending")
    assert report.rows == [
        {"race": "Asian", "_col_1": 4},
        {"race": "White", "_col_1": 6},
    ]
    assert report.notes == [floors.T5_SUPPRESSION_NOTE]
    assert "Black or African American" not in str(report.rows)


def test_multi_key_t5_breakdown_suppresses_sub_floor_cells():
    report = executes(
        "SELECT region, sex, COUNT(*) FROM customers GROUP BY region, sex", "fair_lending"
    )
    assert report.rows == [
        {"region": "EAST", "sex": "F", "_col_2": 3},
        {"region": "WEST", "sex": "F", "_col_2": 3},
        {"region": "WEST", "sex": "M", "_col_2": 3},
    ]
    assert report.notes == [floors.T5_SUPPRESSION_NOTE]


def test_sanctioned_region_breakdown_reports():
    report = executes("SELECT region, COUNT(*) FROM customers GROUP BY region", "fair_lending")
    assert {row["region"]: row["_col_1"] for row in report.rows} == {
        "CENTRAL": 2,
        "EAST": 5,
        "WEST": 6,
    }
    assert report.notes == []


def test_sanctioned_income_band_group_dropped_with_note():
    report = executes(
        "SELECT income_band, COUNT(*) FROM customers GROUP BY income_band", "fair_lending"
    )
    # lower-middle covers only c011 — dropped behind the note; the rest survive at k>=2
    assert {row["income_band"]: row["_col_1"] for row in report.rows} == {
        "high": 4,
        "middle": 5,
        "upper-middle": 3,
    }
    assert report.notes == [floors.K_SUPPRESSION_NOTE]


def test_sanctioned_region_filter_reports():
    """A filtered sanctioned breakdown: WEST+EAST population 11, cells 6/5."""
    report = executes(
        "SELECT sex, COUNT(*) FROM customers WHERE region IN ('WEST', 'EAST') GROUP BY sex",
        "fair_lending",
    )
    assert report.rows == [{"sex": "F", "_col_1": 6}, {"sex": "M", "_col_1": 5}]


def test_fair_lending_row_grain_refused_as_aggregate_only():
    """L13 mechanism: even allowlisted columns never resolve individuals."""
    for sql in ("SELECT race FROM customers", "SELECT region FROM customers"):
        refuses(sql, "fair_lending", policy.AGGREGATE_ONLY)


def test_fair_lending_t5_filter_refused():
    """A T5-filtered count is a single-cell breakdown that bypasses the cell floor."""
    refuses(
        "SELECT COUNT(*) FROM customers WHERE sex = 'F'",
        "fair_lending",
        policy.AGGREGATE_ONLY,
    )


def test_fair_lending_unsanctioned_grouping_key_refused():
    refuses(
        "SELECT zip3, COUNT(*) FROM customers GROUP BY zip3",
        "fair_lending",
        policy.AGGREGATE_ONLY,
    )


def test_fair_lending_non_count_aggregate_refused():
    refuses(
        "SELECT region, AVG(annual_income_usd) FROM customers GROUP BY region",
        "fair_lending",
        policy.AGGREGATE_ONLY,
    )


def test_fair_lending_business_join_uses_normal_pipeline():
    report = executes(
        "SELECT AVG(t.amount_minor) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id",
        "fair_lending",
    )
    assert len(report.rows) == 1
    assert report.notes == []


def test_fair_lending_nongrouped_count_reports_population():
    report = executes("SELECT COUNT(*) FROM customers", "fair_lending")
    assert report.rows[0]["_col_0"] == 13


def test_fair_lending_empty_scope_count_refuses():
    """k<2 refuses uniformly (k=0 included): a refusal never signals 'exactly one'."""
    refuses(
        "SELECT COUNT(*) FROM customers WHERE region = 'CENTRAL' AND segment = 'business'",
        "fair_lending",
        policy.FLOOR_K_ANONYMITY,
    )


# ------------------------------------------ T5 outside the sanctioned path (any grain)


def test_fair_lending_business_grain_t5_grouping_refuses():
    """The 2026-09-15 grain-exemption hole: grouping business rows by a customer T5
    attribute skipped the floors stage entirely, breaking T5 down without floors."""
    report = refuses(
        "SELECT c.sex, COUNT(*) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id GROUP BY c.sex",
        "fair_lending",
        policy.FLOOR_PROTECTED_CLASS,
    )
    # The stopped rewritten statement is recorded (scope-wrapped, never the original).
    assert report.sql_executed is not None
    assert "deleted_at" in report.sql_executed


def test_fair_lending_business_grain_t5_filter_refuses():
    """A T5 predicate over business rows is the same breakdown in filter position."""
    refuses(
        "SELECT t.channel, COUNT(*) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id "
        "WHERE c.sex = 'F' GROUP BY t.channel",
        "fair_lending",
        policy.FLOOR_PROTECTED_CLASS,
    )


def test_fair_lending_business_grain_t5_via_cte_refuses():
    """A T5 reference must be caught through CTE mediation, not just direct columns."""
    refuses(
        "WITH fan AS ("
        "SELECT t.channel, c.sex FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id) "
        "SELECT channel, COUNT(*) FROM fan GROUP BY channel, sex",
        "fair_lending",
        policy.FLOOR_PROTECTED_CLASS,
    )


@pytest.mark.parametrize("role", ["analyst", "reviewer"])
def test_t5_business_grain_grouping_denied_for_roles_without_t5(role):
    """Same statement shape for roles that lack T5: plain column denial (unchanged)."""
    refuses(
        "SELECT c.sex, COUNT(*) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id GROUP BY c.sex",
        role,
        policy.COLUMN_DENIED,
    )


def test_fair_lending_business_grain_non_t5_grouping_succeeds():
    """Over-refusal guard: legitimate business metrics keep the normal pipeline."""
    report = executes(
        "SELECT t.channel, COUNT(*) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id GROUP BY t.channel",
        "fair_lending",
    )
    # 17 in-scope transactions: t010/t019 excluded with their offboarded customers.
    assert {row["channel"]: row["_col_1"] for row in report.rows} == {
        "ach": 7,
        "card": 4,
        "check": 1,
        "wire": 5,
    }
    assert report.notes == []


def test_fair_lending_business_grain_currency_and_rule_grouping_succeeds():
    report = executes(
        "SELECT t.currency, COUNT(*) FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id GROUP BY t.currency",
        "fair_lending",
    )
    assert {row["currency"]: row["_col_1"] for row in report.rows} == {
        "EUR": 1,
        "MXN": 1,
        "USD": 15,
    }
    report = executes(
        "SELECT a.rule_id, COUNT(*) FROM alerts a "
        "JOIN transactions t ON a.txn_id = t.txn_id "
        "JOIN customers c ON t.customer_id = c.customer_id GROUP BY a.rule_id",
        "fair_lending",
    )
    # 13 in-scope alerts: a014 rides the excluded offboarded customer c004's transaction.
    assert {row["rule_id"]: row["_col_1"] for row in report.rows} == {
        "R-CARD-TESTING": 1,
        "R-SANCTIONS": 3,
        "R-STRUCTURING": 5,
        "R-VELOCITY": 4,
    }
    assert report.notes == []


# ---------------------------------------------------------------- T5 boundary conditions (pure)


def test_t5_population_boundary_ten_succeeds():
    """VAL-CORR-027: population exactly 10 is reportable."""
    cells, note = floors.t5_reportable_cells(10, [6, 4])
    assert cells == [6, 4] and note is None


def test_t5_population_boundary_nine_refuses():
    with pytest.raises(policy._PolicyRefusal) as excinfo:
        floors.t5_reportable_cells(9, [5, 4])
    assert excinfo.value.refusal.category == policy.FLOOR_PROTECTED_CLASS


def test_t5_cell_boundary_three_reported():
    cells, note = floors.t5_reportable_cells(10, [7, 3])
    assert cells == [7, 3] and note is None


def test_t5_cell_boundary_two_suppressed():
    cells, note = floors.t5_reportable_cells(11, [7, 2, 2])
    assert cells == [7]
    assert note == floors.T5_SUPPRESSION_NOTE


def test_t5_single_suppressed_cell_refuses():
    with pytest.raises(policy._PolicyRefusal) as excinfo:
        floors.t5_reportable_cells(10, [8, 2])
    assert excinfo.value.refusal.category == policy.FLOOR_PROTECTED_CLASS


def test_t5_cells_8_4_1_refuse_whole_breakdown():
    """VAL-CORR-028: 13 - 8 - 4 = 1 — the suppressed cell is derivable, so refuse all."""
    with pytest.raises(policy._PolicyRefusal) as excinfo:
        floors.t5_reportable_cells(13, [8, 4, 1])
    assert excinfo.value.refusal.category == policy.FLOOR_PROTECTED_CLASS


def test_t5_note_states_no_sizes():
    assert re.findall(r"\d+", floors.T5_SUPPRESSION_NOTE) == ["3"]  # the policy threshold only
