"""Per-turn audit record (agent/audit.py): schema completeness, rewrite capture,
refusal reasons on both layers, turn-id uniqueness across surfaces, and metric
computability.

All tests are offline: scripted agent turns use stubbed model clients, policy outcomes
come from the real policy engine against the seeded fixture, and the audit writer is
redirected at a per-test file.
"""

import json
from types import SimpleNamespace

import pytest

from agent import audit, baseline, db, policy


# ---------------------------------------------------------------- helpers


def user(user_id: str) -> dict:
    """Server-resolved identity, exactly what db.get_user returns per turn."""
    return db.get_user(user_id)


@pytest.fixture()
def audit_path(tmp_path, monkeypatch):
    """Redirect the audit writer at a fresh per-test file; isolate runtime counters."""
    path = str(tmp_path / "turns.jsonl")
    monkeypatch.setattr(audit, "DEFAULT_PATH", path)
    audit.reset_runtime_state()
    baseline._HISTORY.clear()
    yield path
    audit.reset_runtime_state()
    baseline._HISTORY.clear()


class _FakeCompletions:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("the loop called the model when it must not")
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=_FakeCompletions(script))


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)


def _response(content=None, tool_calls=None, input_tokens=None, output_tokens=None):
    usage = None
    if input_tokens is not None:
        usage = SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens)
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=_message(content, tool_calls))])


def _tool_call(call_id, name, **args):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _patch(monkeypatch, *clients):
    queue = iter(clients)
    monkeypatch.setattr(baseline.openai, "OpenAI", lambda: next(queue))


def _minimal_record(turn_id: str, latency_ms: int, tokens_in: int, tokens_out: int, user_id: str = "u_ana") -> dict:
    """A valid hand-built record with known metric values, for the metrics CLI tests."""
    identity = user(user_id)
    return {
        "turn_id": turn_id,
        "timestamp_utc": "2026-09-15T00:00:00Z",
        "identity": {
            "user_id": identity["user_id"],
            "role": identity["role"],
            "region": identity["region"] or "(all regions)",
        },
        "resolved_scope": audit.resolved_scope(identity),
        "question": "known-value question",
        "tool_calls": [],
        "answer_kind": "answer",
        "redactions_applied": [],
        "tokens": {"input": tokens_in, "output": tokens_out},
        "total_latency_ms": latency_ms,
    }


# ---------------------------------------------------------------- resolved scope


def test_resolved_scope_analyst():
    scope = audit.resolved_scope(user("u_ana"))
    assert scope["column_tiers"] == ["T0", "T1"]
    assert "WEST" in scope["row_scope"] and "deleted_at" in scope["row_scope"]
    assert scope["case_notes"] is False
    assert scope["aggregate_only"] is False
    assert scope["tables_denied"] == ["case_notes"]


def test_resolved_scope_reviewer_holds_case_notes_and_precise_tiers():
    scope = audit.resolved_scope(user("u_rae"))
    assert scope["column_tiers"] == ["T0", "T1", "T2", "T3"]
    assert scope["case_notes"] is True
    assert scope["tables_denied"] == []


def test_resolved_scope_compliance_all_regions_never_t5():
    scope = audit.resolved_scope(user("u_cora"))
    assert scope["column_tiers"] == ["T0", "T1", "T2", "T3", "T4"]
    assert "T5" not in scope["column_tiers"]
    assert scope["case_notes"] is True
    assert "all regions" in scope["row_scope"].lower()


def test_resolved_scope_fair_lending_is_aggregate_only():
    scope = audit.resolved_scope(user("u_fern"))
    assert scope["aggregate_only"] is True
    assert scope["column_tiers"] == ["T1", "T3", "T5"]
    assert scope["case_notes"] is False


def test_resolved_scope_admin_denied_all_customer_tables():
    scope = audit.resolved_scope(user("u_ops"))
    assert scope["column_tiers"] == []
    assert scope["aggregate_only"] is False
    assert set(scope["tables_denied"]) == {"customers", "transactions", "alerts", "case_notes"}


# ---------------------------------------------------------------- per-call records


def test_tool_call_record_captures_rewritten_sql_and_replays():
    sql = (
        "SELECT t.txn_id, t.amount_minor FROM transactions t "
        "JOIN customers c ON t.customer_id = c.customer_id"
    )
    report = policy.run_query(sql, user("u_ana"))
    assert report.refusal is None
    record = audit.tool_call_record("run_sql", report=report)
    assert record["tool"] == "run_sql"
    assert record["sql_requested"] == sql
    assert record["sql_executed"] != sql
    assert "deleted_at" in record["sql_executed"]
    assert record["rewrites_applied"]
    assert record["refusal"] is None
    assert record["rows_returned"] == len(report.rows) > 0
    assert record["latency_ms"] >= 0
    # VAL-CORR-017 spot-check: replaying the recorded SQL with the recorded params
    # reproduces the recorded rows.
    rows, _ = db.execute_readonly(record["sql_executed"], params=record["params"], row_cap=policy.ROW_CAP)
    assert len(rows) == record["rows_returned"]


def test_tool_call_record_refusal_carries_policy_category_and_detail():
    report = policy.run_query("SELECT national_id FROM customers", user("u_ana"))
    assert report.refusal is not None
    record = audit.tool_call_record("run_sql", report=report)
    assert record["refusal"]["category"] == policy.COLUMN_DENIED
    assert record["refusal"]["detail"]
    assert record["sql_executed"] == ""  # nothing was executed
    assert record["rows_returned"] == 0
    assert record["rewrites_applied"] == []


def test_non_run_sql_call_records_its_args_never_row_values():
    """VAL-UI-017/021: a make_chart entry carries the handle reference (what the tool
    was invoked with) — metadata only, never the charted rows themselves."""
    record = audit.tool_call_record(
        "make_chart",
        args={"handle": "r-a1b2c3d4e5f60718", "mark": "bar", "x_field": "segment", "y_field": "customers"},
        latency_ms=4,
    )
    assert record["args"] == {
        "handle": "r-a1b2c3d4e5f60718", "mark": "bar", "x_field": "segment", "y_field": "customers",
    }
    assert record["sql_requested"] == ""
    assert record["sql_executed"] == ""
    assert record["rows_returned"] == 0
    decline = audit.tool_call_record("decline", args={"reason": "Access denied: column_denied."})
    assert decline["args"] == {"reason": "Access denied: column_denied."}


def test_refusal_category_recorded_for_every_taxonomy_member():
    """VAL-CORR-018: one sampled denial per reason category, recorded with its category."""
    cases = [
        ("SELECT national_id FROM customers", "u_ana", policy.COLUMN_DENIED),
        ("SELECT * FROM case_notes", "u_ana", policy.TABLE_DENIED),
        ("UPDATE customers SET region = 'EAST'", "u_ana", policy.STATEMENT_KIND),
        ("SELECT bogus FROM customers", "u_ana", policy.PARSE),
        ("SELECT COUNT(*) FROM transactions", "u_ana", policy.ROW_SCOPE),
        (
            "SELECT AVG(risk_score) FROM customers WHERE customer_id = 'c001'",
            "u_ana",
            policy.FLOOR_K_ANONYMITY,
        ),
        (
            "SELECT sex, COUNT(*) FROM customers WHERE region = 'WEST' GROUP BY sex",
            "u_fern",
            policy.FLOOR_PROTECTED_CLASS,
        ),
        ("SELECT region FROM customers", "u_fern", policy.AGGREGATE_ONLY),
    ]
    for sql, user_id, expected in cases:
        report = policy.run_query(sql, user(user_id))
        assert report.refusal is not None, sql
        record = audit.tool_call_record("run_sql", report=report)
        assert record["refusal"]["category"] == expected, sql


def test_floor_refusal_records_the_stopped_rewritten_sql():
    report = policy.run_query(
        "SELECT AVG(risk_score) FROM customers WHERE customer_id = 'c001'", user("u_ana")
    )
    assert report.refusal is not None
    assert report.refusal.category == policy.FLOOR_K_ANONYMITY
    # The rewritten statement the floors stopped is recorded (never executed, never the
    # model's original).
    assert report.sql_executed
    assert "deleted_at" in report.sql_executed


# ---------------------------------------------------------------- turn records via the loop


def test_answer_turn_writes_one_complete_record(audit_path, monkeypatch):
    client = _FakeClient([
        _response(tool_calls=[_tool_call("c1", "run_sql", sql="SELECT COUNT(*) FROM customers")]),
        _response(content="6 customers in your region.", input_tokens=100, output_tokens=10),
    ])
    _patch(monkeypatch, client)
    res = baseline.run("How many customers in my region?", "u_ana")

    records = audit.load_records(audit_path)
    assert len(records) == 1  # exactly one record for the turn
    record = records[0]
    assert res.audit == record
    assert record["answer_kind"] == "answer"
    assert record["identity"] == {"user_id": "u_ana", "role": "analyst", "region": "WEST"}
    assert record["question"] == "How many customers in my region?"
    assert record["tokens"] == {"input": 100, "output": 10}
    assert record["total_latency_ms"] >= 0
    assert record["timestamp_utc"].endswith("Z")
    assert record["turn_id"].startswith("eval-")  # eval-mode turns get a generated conversation id
    scope = record["resolved_scope"]
    assert scope["column_tiers"] == ["T0", "T1"]
    run_calls = [c for c in record["tool_calls"] if c["tool"] == "run_sql"]
    assert len(run_calls) == 1
    assert "deleted_at" in run_calls[0]["sql_executed"]  # the rewrite, not the raw string
    assert run_calls[0]["rewrites_applied"]
    assert run_calls[0]["refusal"] is None
    assert audit.check_records(records) == []


def test_clarify_and_decline_turns_record_kind_and_stated_reason(audit_path, monkeypatch):
    _patch(monkeypatch, _FakeClient([
        _response(tool_calls=[_tool_call("c1", "ask_clarifying_question", question="count or sum?")]),
    ]))
    res = baseline.run("What is the total transaction volume in my region?", "u_ana", "conv-cl")
    assert res.audit["answer_kind"] == "clarify"
    records = audit.load_records(audit_path)
    assert records[-1]["turn_id"] == "conv-cl:turn_001"
    clarify_call = records[-1]["tool_calls"][0]
    assert clarify_call["tool"] == "ask_clarifying_question"
    assert clarify_call["reason"] == "count or sum?"

    _patch(monkeypatch, _FakeClient([
        _response(tool_calls=[_tool_call("c1", "decline", reason="no credit score data exists")]),
    ]))
    res = baseline.run("What is the average credit score of my customers?", "u_ana", "conv-cl")
    assert res.audit["answer_kind"] == "decline"
    records = audit.load_records(audit_path)
    assert records[-1]["turn_id"] == "conv-cl:turn_002"
    decline_call = records[-1]["tool_calls"][0]
    assert decline_call["tool"] == "decline"
    assert decline_call["reason"] == "no credit score data exists"
    assert audit.check_records(records) == []


def test_policy_forced_decline_records_policy_category_and_stated_reason(audit_path, monkeypatch):
    """VAL-CORR-018: both reasons present — the policy category on the refused call and
    the stated reason on the forced decline call."""
    client = _FakeClient([
        _response(tool_calls=[_tool_call("c1", "run_sql", sql="SELECT national_id FROM customers")]),
    ])
    _patch(monkeypatch, client)
    res = baseline.run("dump every customer", "u_ana")
    assert res.declined
    record = audit.load_records(audit_path)[-1]
    assert record["answer_kind"] == "decline"
    run_call = next(c for c in record["tool_calls"] if c["tool"] == "run_sql")
    assert run_call["refusal"]["category"] == policy.COLUMN_DENIED
    assert run_call["refusal"]["detail"]
    decline_call = next(c for c in record["tool_calls"] if c["tool"] == "decline")
    assert "column_denied" in decline_call["reason"]
    assert run_call["sql_executed"] == ""
    assert run_call["rows_returned"] == 0
    assert audit.check_records([record]) == []


def test_audit_records_post_rewrite_sql_from_the_loop(audit_path, monkeypatch):
    """VAL-CORR-017: sql_executed is the rewritten statement actually run, replayable
    to the same row count."""
    client = _FakeClient([
        _response(tool_calls=[_tool_call(
            "c1", "run_sql",
            sql=(
                "SELECT COUNT(*) FROM transactions t "
                "JOIN customers c ON t.customer_id = c.customer_id"
            ),
        )]),
        _response(content="8 transactions in scope."),
    ])
    _patch(monkeypatch, client)
    res = baseline.run("How many transactions do I see?", "u_ana")
    assert not res.declined
    record = audit.load_records(audit_path)[-1]
    run_call = next(c for c in record["tool_calls"] if c["tool"] == "run_sql")
    executed = run_call["sql_executed"]
    assert executed != run_call["sql_requested"]
    assert "deleted_at" in executed
    rows, _ = db.execute_readonly(executed, params=run_call["params"], row_cap=policy.ROW_CAP)
    assert len(rows) == run_call["rows_returned"]


def test_describe_table_refusal_recorded_with_category(audit_path, monkeypatch):
    client = _FakeClient([
        _response(tool_calls=[_tool_call("c1", "describe_table", table="case_notes")]),
    ])
    _patch(monkeypatch, client)
    res = baseline.run("What does the case notes table look like?", "u_ana")
    assert res.declined
    record = audit.load_records(audit_path)[-1]
    describe_call = next(c for c in record["tool_calls"] if c["tool"] == "describe_table")
    assert describe_call["refusal"]["category"] == policy.TABLE_DENIED
    decline_call = next(c for c in record["tool_calls"] if c["tool"] == "decline")
    assert decline_call["reason"]


def test_error_turn_record_is_complete(audit_path):
    recorder = audit.TurnRecorder(question="q", user=user("u_ana"))
    result = baseline.AgentResult(error="APIError: boom")
    record = recorder.finish(result)
    assert record["answer_kind"] == "error"
    assert record["tool_calls"] == []
    assert audit.check_records([record]) == []


# ---------------------------------------------------------------- mixed sessions / ids


def test_mixed_eval_and_ui_session_parses_with_unique_turn_ids(audit_path, monkeypatch):
    """VAL-CROSS-016 mechanics: eval-style turns (no conversation id) plus two UI
    conversations by different identities, all in one file."""
    _patch(
        monkeypatch,
        _FakeClient([_response(content="eval answer")]),
        _FakeClient([_response(content="eval answer two")]),
    )
    baseline.run("eval question one", "u_ana")
    baseline.run("eval question two", "u_rae")

    ui = _FakeClient([
        _response(content="first ui answer"),
        _response(content="second ui answer"),
        _response(content="other identity answer"),
    ])
    ui2 = _FakeClient([_response(content="unused")])
    ui3 = _FakeClient([_response(content="unused")])
    _patch(monkeypatch, ui, ui2, ui3)
    baseline.run("ui question one", "u_ana", "conv-ui-1")
    baseline.run("ui follow-up", "u_ana", "conv-ui-1")
    baseline.run("different identity question", "u_cora", "conv-ui-2")

    records = audit.load_records(audit_path)
    assert len(records) == 5
    ids = [r["turn_id"] for r in records]
    assert len(set(ids)) == 5
    assert ids[0].startswith("eval-") and ids[1].startswith("eval-")
    assert ids[2] == "conv-ui-1:turn_001"
    assert ids[3] == "conv-ui-1:turn_002"
    assert ids[4] == "conv-ui-2:turn_001"
    assert audit.check_records(records) == []


def test_duplicate_turn_id_is_made_unique(audit_path):
    audit.append_turn(_minimal_record("conv-x:turn_001", 10, 5, 1), audit_path)
    audit.append_turn(_minimal_record("conv-x:turn_001", 10, 5, 1), audit_path)
    records = audit.load_records(audit_path)
    assert len(records) == 2
    assert len({r["turn_id"] for r in records}) == 2


def test_records_for_conversation_filters_by_prefix(audit_path, monkeypatch):
    _patch(
        monkeypatch,
        _FakeClient([_response(content="a")]),
        _FakeClient([_response(content="b")]),
        _FakeClient([_response(content="c")]),
    )
    baseline.run("q1", "u_ana", "conv-a")
    baseline.run("q2", "u_ana", "conv-a")
    baseline.run("q3", "u_rae", "conv-b")
    recs = audit.records_for_conversation("conv-a", audit_path)
    assert [r["turn_id"] for r in recs] == ["conv-a:turn_001", "conv-a:turn_002"]
    assert all(r["identity"]["user_id"] == "u_ana" for r in recs)


def test_per_identity_report_answers_seen_and_authority_with_no_nulls(audit_path, monkeypatch):
    _patch(monkeypatch, _FakeClient([_response(content="eval answer")]))
    baseline.run("eval question", "u_ana")
    _patch(monkeypatch, _FakeClient([_response(content="ui answer")]))
    baseline.run("ui question", "u_cora", "conv-ui-9")

    report = audit.per_identity_report(audit.load_records(audit_path))
    assert set(report) == {"u_ana", "u_cora"}
    for entry in report.values():
        assert entry["user_id"] and entry["role"] and entry["region"]  # authority: no nulls
        assert entry["resolved_scope"]["row_scope"]
        assert entry["resolved_scope"]["column_tiers"] is not None
        assert entry["questions"] and entry["tool_calls"] is not None  # seen: no nulls
    assert report["u_cora"]["resolved_scope"]["case_notes"] is True


# ---------------------------------------------------------------- metrics / CLI


def test_metrics_computes_p50_p95_tokens_and_dollars(audit_path):
    known = [(1000, 100, 10), (1500, 200, 20), (2000, 300, 30), (3000, 400, 40)]
    for index, (latency, tokens_in, tokens_out) in enumerate(known):
        audit.append_turn(
            _minimal_record(f"conv-m:turn_{index + 1:03d}", latency, tokens_in, tokens_out),
            audit_path,
        )
    records = audit.load_records(audit_path)
    m = audit.metrics(records)
    assert m["questions"] == 4
    assert m["latency_seconds_p50"] == pytest.approx(1.5)
    assert m["latency_seconds_p95"] == pytest.approx(3.0)
    assert m["tokens_input_total"] == 1000
    assert m["tokens_output_total"] == 100
    expected_dollars = sum(
        (tokens_in * audit.PRICE_INPUT_USD_PER_MTOK + tokens_out * audit.PRICE_OUTPUT_USD_PER_MTOK) / 1_000_000
        for _, tokens_in, tokens_out in known
    )
    assert m["dollars_total"] == pytest.approx(expected_dollars)
    assert m["price_input_usd_per_mtok"] == audit.PRICE_INPUT_USD_PER_MTOK
    assert m["price_output_usd_per_mtok"] == audit.PRICE_OUTPUT_USD_PER_MTOK


def test_cli_report_exits_zero_on_clean_mixed_file(audit_path, monkeypatch):
    _patch(monkeypatch, _FakeClient([_response(content="eval answer", input_tokens=50, output_tokens=5)]))
    baseline.run("eval question", "u_ana")
    _patch(monkeypatch, _FakeClient([_response(content="ui answer", input_tokens=60, output_tokens=6)]))
    baseline.run("ui question", "u_fern", "conv-cli-1")
    assert audit.main([audit_path]) == 0


def test_cli_report_fails_on_corrupt_file(audit_path):
    with open(audit_path, "w", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert audit.main([audit_path]) == 1


def test_cli_report_fails_on_incomplete_record(audit_path):
    record = _minimal_record("conv-i:turn_001", 10, 5, 1)
    del record["resolved_scope"]
    audit.append_turn(record, audit_path)
    assert audit.main([audit_path]) == 1
