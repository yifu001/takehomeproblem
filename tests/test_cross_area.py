"""Cross-area coherence — the API-free half of the cross-surface battery.

The live halves (eval CLI, chat UI via agent-browser, real /chat turns) are the
feature's runtime evidence; these tests pin, at zero API cost, the representations
that must agree across surfaces:

  - the refusal-category taxonomy is defined once (agent/policy.py): no agent,
    interface, or frontend module carries its own category string (VAL-CROSS-017);
  - every refusal the REAL policy engine produces — probed through the real tool
    dispatch with hostile SQL — is a taxonomy member in its normalized,
    non-echoing tool text (VAL-CROSS-017, VAL-CROSS-010);
  - the API layer marks answer / clarify / decline / error distinctly and the audit
    record's answer_kind agrees with the API JSON for every kind (VAL-CROSS-008);
  - a genuinely empty in-scope result stays answer_kind=answer — never collapsed
    into a decline (VAL-CROSS-008);
  - a clarify loop is two turns in ONE conversation — clarify then answer, with
    separate audit records in order (VAL-CROSS-008, VAL-UI-020's API half);
  - the identity lifecycle keeps conversations bound and audit trails separate
    per identity end-to-end (VAL-CROSS-002's API half).
"""

import ast
import json
import pathlib

import pytest
from agent import audit, baseline, db, policy, tools
from fastapi.testclient import TestClient

import interface.app as interface_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

TAXONOMY = {
    policy.COLUMN_DENIED,
    policy.ROW_SCOPE,
    policy.TABLE_DENIED,
    policy.STATEMENT_KIND,
    policy.PARSE,
    policy.TIMEOUT,
    policy.FLOOR_K_ANONYMITY,
    policy.FLOOR_PROTECTED_CLASS,
    policy.AGGREGATE_ONLY,
}


# ---------------------------------------------------------------- fixtures / helpers


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """TestClient over the real app with the audit file redirected to tmp."""
    monkeypatch.setattr(interface_app, "AUDIT_PATH", str(tmp_path / "turns.jsonl"))
    interface_app._CONVERSATIONS.clear()
    baseline._HISTORY.clear()
    audit.reset_runtime_state()
    tools.clear_result_sets()
    yield TestClient(interface_app.app)
    interface_app._CONVERSATIONS.clear()
    baseline._HISTORY.clear()
    audit.reset_runtime_state()
    tools.clear_result_sets()


def install_fake_runner(monkeypatch, outcomes):
    """Replace baseline.run with a fake replaying `outcomes` (one AgentResult-shaped
    dict per call, in order) while writing real-format audit records."""
    calls: list[dict] = []

    def fake_run(question: str, user_id: str, conversation_id: str | None = None) -> baseline.AgentResult:
        calls.append({"question": question, "user_id": user_id, "conversation_id": conversation_id})
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        result = baseline.AgentResult(
            answer=outcome.get("answer", ""),
            sql_log=list(outcome.get("sql_log", [])),
            declined=outcome.get("declined", False),
            clarified=outcome.get("clarified", False),
            error=outcome.get("error"),
            tool_calls=list(outcome.get("tool_calls", [])),
            transcript=list(outcome.get("transcript", [])),
        )
        recorder = audit.TurnRecorder(
            question=question,
            user=db.get_user(user_id),
            conversation_id=conversation_id,
            path=interface_app.AUDIT_PATH,
        )
        result.audit = recorder.finish(result)
        return result

    monkeypatch.setattr(baseline, "run", fake_run)
    return calls


# ---------------------------------------------------------------- taxonomy single-sourcing


def _non_docstring_string_literals(path: pathlib.Path) -> set[str]:
    """String constants in code, excluding module/class/function docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings
    }


def test_refusal_taxonomy_defined_once_in_policy():
    """VAL-CROSS-017: category strings exist as literals ONLY in policy.py (the
    definition site); every other module references the imported constants.

    AGGREGATE_ONLY is excluded from the literal scan because the audit's
    resolved_scope schema legitimately carries a FIELD named "aggregate_only"
    (architecture §6) — the category itself is covered by the behavioral
    membership test below. row_scope is excluded for the same reason: it is
    likewise a resolved_scope field name."""
    scanned = TAXONOMY - {policy.AGGREGATE_ONLY, policy.ROW_SCOPE}
    for rel in (
        "agent/floors.py",
        "agent/tools.py",
        "agent/baseline.py",
        "agent/audit.py",
        "agent/db.py",
        "interface/app.py",
    ):
        leaked = _non_docstring_string_literals(REPO_ROOT / rel) & scanned
        assert not leaked, f"{rel} hard-codes refusal categories {sorted(leaked)}"


def test_frontend_has_no_local_category_table():
    """VAL-CROSS-017: the UI's refusal card renders the audit record's category —
    it carries no local copy of the taxonomy that could drift from the audit.
    row_scope/aggregate_only appear in app.js only as resolved_scope FIELD accesses,
    never as refusal categories (excluded here, covered behaviorally above)."""
    app_js = (REPO_ROOT / "interface" / "static" / "app.js").read_text(encoding="utf-8")
    for category in TAXONOMY - {policy.ROW_SCOPE, policy.AGGREGATE_ONLY}:
        assert category not in app_js, f"app.js hard-codes the category {category!r}"
    assert "refusalCategory(" in app_js  # the card reads it from the audit record
    assert "refusal-category" in app_js  # ...and renders it in the card's category node


def test_real_engine_refusals_are_taxonomy_members():
    """VAL-CROSS-017/010: hostile SQL through the REAL tool dispatch (zero API) —
    every refusal is a taxonomy member, in the normalized non-echoing text form."""
    probes = [
        ("u_ana", "SELECT national_id, email FROM customers"),               # T4 for analyst
        ("u_ana", "SELECT COUNT(*) FROM customers WHERE risk_score > 90"),   # k=1 aggregate
        ("u_ana", "SELECT full_name FROM customers WHERE annual_income_usd > 100000"),  # T3 filter
        ("u_ana", "UPDATE customers SET full_name = 'x'"),                   # non-SELECT
        ("u_ana", "SELECT * FROM customers; DROP TABLE customers"),          # multi-statement
        ("u_ana", "SELETC nope FROM customers"),                             # unparsable
        ("u_ops", "SELECT full_name FROM customers"),                        # admin: table denied
        ("u_ops", "SELECT COUNT(*) FROM transactions"),                      # admin: unreachable join
        ("u_fern", "SELECT full_name FROM customers"),                       # fair_lending: row grain
        ("u_fern", "SELECT region, COUNT(*) AS n FROM customers"),           # fair_lending: off-sanctioned-path shape
        ("u_rae", "SELECT race FROM customers"),                             # T5 for reviewer
    ]
    seen: set[str] = set()
    for user_id, sql in probes:
        text, report = tools.run_sql_tool(sql, db.get_user(user_id))
        assert report.refusal is not None, f"{user_id}: {sql}"
        category = report.refusal.category
        seen.add(category)
        assert category in TAXONOMY, category
        assert text.startswith("error: query refused by access policy ("), text
        assert text.endswith(")"), text
        # no echo: the model's SQL and any driver error text never reach the result
        assert "SELECT" not in text and "customers" not in text, text
        assert "sqlite" not in text.lower() and "OperationalError" not in text, text
    assert {policy.COLUMN_DENIED, policy.FLOOR_K_ANONYMITY, policy.STATEMENT_KIND,
            policy.PARSE, policy.TABLE_DENIED, policy.AGGREGATE_ONLY} <= seen


def test_chart_handles_reject_foreign_identity_and_fabricated_rows():
    """VAL-CROSS-005 (tool boundary): a handle minted for one identity is not chartable
    by another, and hand-written rows have no path into make_chart at all."""
    _text, report = tools.run_sql_tool(
        "SELECT segment, COUNT(*) AS n FROM customers GROUP BY segment", db.get_user("u_ana")
    )
    assert report.refusal is None
    handle = "r-" + "0123456789abcdef"
    import agent.tools as tools_module

    tools_module._RESULT_SETS[handle] = {"user_id": "u_ana", "rows": report.rows}
    foreign = tools.dispatch(
        "make_chart",
        {"handle": handle, "mark": "bar", "x_field": "segment", "y_field": "n"},
        user=db.get_user("u_cora"),
    )
    assert foreign.startswith("error: chart refused"), foreign
    fabricated = tools.dispatch(
        "make_chart",
        {"handle": "r-deadbeefdeadbeef", "mark": "bar", "x_field": "segment", "y_field": "n",
         "rows": [{"segment": "retail", "n": 999}]},
        user=db.get_user("u_ana"),
    )
    assert fabricated.startswith("error: chart refused"), fabricated
    own = tools.dispatch(
        "make_chart",
        {"handle": handle, "mark": "bar", "x_field": "segment", "y_field": "n"},
        user=db.get_user("u_ana"),
    )
    assert own.startswith("chart rendered:"), own


# ---------------------------------------------------------------- four answer kinds


def test_four_answer_kinds_distinct_and_audit_agrees(client, monkeypatch):
    """VAL-CROSS-008: the API marks each kind in the same answer_kind field and every
    audit record agrees with the API JSON for the same turn."""
    cases = [
        ("answer", {"answer": "In your region there are 6 active customers."}),
        ("clarify", {"answer": "Count or sum — which did you mean?", "clarified": True}),
        ("decline", {"answer": "Access denied: your role cannot read national ids.", "declined": True}),
        ("error", {"answer": "", "error": "APIError: simulated outage"}),
    ]
    install_fake_runner(monkeypatch, [outcome for _, outcome in cases])
    seen_kinds = set()
    for expected_kind, _outcome in cases:
        body = client.post("/chat", json={"user_id": "u_ana", "question": f"probe {expected_kind}"}).json()
        seen_kinds.add(body["answer_kind"])
        assert body["answer_kind"] == expected_kind
        assert body["audit"]["answer_kind"] == expected_kind  # audit == API representation
    assert seen_kinds == {"answer", "clarify", "decline", "error"}


def test_empty_result_not_collapsed_into_decline(client, monkeypatch):
    """VAL-CROSS-008: an in-scope query legitimately matching zero rows is an answer
    turn (rows_returned 0, no refusal) — the API never turns it into a decline."""
    install_fake_runner(monkeypatch, [{
        "answer": "No wire transactions above $500,000 USD were found in your region.",
        "sql_log": ["SELECT 1 WHERE 0"],
    }])
    body = client.post("/chat", json={"user_id": "u_ana", "question": "empty probe"}).json()
    assert body["answer_kind"] == "answer"
    assert body["audit"]["answer_kind"] == "answer"
    assert body["audit"]["tool_calls"] == [] or all(
        call.get("refusal") is None for call in body["audit"]["tool_calls"]
    )


def test_clarify_loop_two_turns_one_conversation(client, monkeypatch):
    """VAL-CROSS-008 (clarify probe): turn 1 clarifies, the user's reply is turn 2 in
    the SAME conversation, and the audit trail shows clarify -> answer in order."""
    install_fake_runner(monkeypatch, [
        {"answer": "Count or sum — and in which currency?", "clarified": True},
        {"answer": "2 wire transactions, $1,215.00 USD.", "sql_log": ["SELECT COUNT(*) FROM t"]},
    ])
    first = client.post("/chat", json={"user_id": "u_ana", "question": "total transaction volume?"})
    conversation_id = first.json()["conversation_id"]
    second = client.post("/chat", json={
        "user_id": "u_ana", "conversation_id": conversation_id, "question": "count, in USD",
    })
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert first.json()["answer_kind"] == "clarify"
    assert second.json()["answer_kind"] == "answer"
    records = client.get(f"/audit/{conversation_id}", params={"user_id": "u_ana"}).json()
    assert [r["answer_kind"] for r in records] == ["clarify", "answer"]


def test_identity_lifecycle_separate_audits_no_cross_canary(client, monkeypatch):
    """VAL-CROSS-002 (API half): each identity's turns bind to their own conversation,
    a cross-identity reuse is a 409 with no audit append, and the two trails carry
    their own identity/scope only."""
    install_fake_runner(monkeypatch, [
        {"answer": "6 active customers in your region."},
        {"answer": "15 customers company-wide, including offboarded."},
    ])
    ana = client.post("/chat", json={"user_id": "u_ana", "question": "how many customers?"}).json()
    rejected = client.post("/chat", json={
        "user_id": "u_cora", "conversation_id": ana["conversation_id"], "question": "same again",
    })
    assert rejected.status_code == 409
    cora = client.post("/chat", json={"user_id": "u_cora", "question": "how many customers?"}).json()
    assert cora["conversation_id"] != ana["conversation_id"]

    ana_records = client.get(f"/audit/{ana['conversation_id']}", params={"user_id": "u_ana"}).json()
    cora_records = client.get(f"/audit/{cora['conversation_id']}", params={"user_id": "u_cora"}).json()
    assert all(r["identity"]["user_id"] == "u_ana" and r["identity"]["role"] == "analyst" for r in ana_records)
    assert all(r["identity"]["user_id"] == "u_cora" and r["identity"]["role"] == "compliance" for r in cora_records)
    assert all(r["resolved_scope"]["row_scope"] == "region='WEST' AND deleted_at IS NULL" for r in ana_records)
    assert all(r["resolved_scope"]["row_scope"] == "all regions, including offboarded customers" for r in cora_records)
    assert "u_cora" not in json.dumps(ana_records)
    assert "u_ana" not in json.dumps(cora_records)
