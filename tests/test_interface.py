"""The FastAPI interface contract (interface/app.py).

These tests pin the HTTP surface offline (no API cost): the /users identity list, the
/chat response contract with a stubbed agent runner, conversation-identity binding
(409), the negative paths (404/422, structured bodies that never echo SQL, data or
stack traces), per-identity audit scoping, and the absence of key material or policy
logic in anything the app serves statically.
"""

import json
import os

import pytest
from agent import audit, baseline, db
from fastapi.testclient import TestClient

import interface.app as interface_app

# A representative post-rewrite statement (the shape the policy engine actually logs:
# scope predicates bound as server-side parameters, never the model's original string).
REWRITTEN_SQL = (
    "SELECT COUNT(*) AS customers FROM (SELECT * FROM customers "
    "WHERE customers.region = :scope_region AND customers.deleted_at IS NULL)"
)

CHART_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "title": "Customers by segment",
    "mark": "bar",
    "data": {"values": [{"segment": "retail", "customers": 4}]},
    "encoding": {"x": {"field": "segment", "type": "nominal"}, "y": {"field": "customers", "type": "quantitative"}},
}

CHART_LINE = f"[tool_result make_chart] chart rendered: {json.dumps(CHART_SPEC)}"


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """TestClient over the real app with the audit file redirected to tmp."""
    monkeypatch.setattr(interface_app, "AUDIT_PATH", str(tmp_path / "turns.jsonl"))
    interface_app._CONVERSATIONS.clear()
    baseline._HISTORY.clear()
    audit.reset_runtime_state()
    yield TestClient(interface_app.app)
    interface_app._CONVERSATIONS.clear()
    baseline._HISTORY.clear()
    audit.reset_runtime_state()


def install_fake_runner(
    monkeypatch,
    *,
    answer: str = "In your region there are 6 active customers.",
    sql_log: list[str] | None = None,
    declined: bool = False,
    clarified: bool = False,
    error: str | None = None,
    tool_calls: list[dict] | None = None,
    transcript: list[str] | None = None,
) -> list[dict]:
    """Replace baseline.run with a fake that records calls and writes a real-format
    audit record through agent/audit.py (the same machinery the live loop uses)."""
    calls: list[dict] = []

    def fake_run(question: str, user_id: str, conversation_id: str | None = None) -> baseline.AgentResult:
        calls.append({"question": question, "user_id": user_id, "conversation_id": conversation_id})
        result = baseline.AgentResult(
            answer=answer,
            sql_log=list(sql_log or []),
            declined=declined,
            clarified=clarified,
            error=error,
            tool_calls=list(tool_calls or []),
            transcript=list(transcript or []),
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


# ---------------------------------------------------------------- GET /api/health, GET /users


def test_health_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_users_lists_six_seeded_identities(client):
    response = client.get("/users")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and len(body) == 6
    with db.connect() as conn:
        stored = [dict(r) for r in conn.execute(
            "SELECT user_id, full_name, role, region FROM users ORDER BY user_id"
        ).fetchall()]
    assert body == stored  # the fixture, exactly as stored
    for entry in body:
        assert set(entry) == {"user_id", "full_name", "role", "region"}  # no extra fields


# ---------------------------------------------------------------- POST /chat contract


def test_chat_returns_full_contract(client, monkeypatch):
    calls = install_fake_runner(monkeypatch, sql_log=[REWRITTEN_SQL])
    response = client.post("/chat", json={"user_id": "u_ana", "question": "How many customers are in my region?"})
    assert response.status_code == 200
    body = response.json()
    for key in ("conversation_id", "answer", "answer_kind", "tool_calls", "sql_executed", "audit"):
        assert key in body, f"missing contract key {key}"
    assert body["answer_kind"] == "answer"
    assert body["sql_executed"] == [REWRITTEN_SQL]  # post-rewrite, not the model's original
    turn = body["audit"]
    assert turn["identity"]["user_id"] == "u_ana"
    assert turn["identity"]["role"] == "analyst"
    assert turn["resolved_scope"]["row_scope"]
    assert turn["question"] == "How many customers are in my region?"
    assert turn["tool_calls"] == []
    assert calls and calls[0]["user_id"] == "u_ana"  # identity resolved server-side


def test_chat_issues_conversation_id_and_reuses_it(client, monkeypatch):
    install_fake_runner(monkeypatch)
    first = client.post("/chat", json={"user_id": "u_ana", "question": "count customers"})
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]
    assert conversation_id
    second = client.post("/chat", json={
        "user_id": "u_ana", "conversation_id": conversation_id, "question": "of those, how many are retail?",
    })
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    records = client.get(f"/audit/{conversation_id}", params={"user_id": "u_ana"}).json()
    assert [r["turn_id"].split(":")[0] for r in records] == [conversation_id, conversation_id]


def test_chat_identity_mismatch_409_and_audit_untouched(client, monkeypatch):
    calls = install_fake_runner(monkeypatch, answer="marker-answer-Zz42")
    first = client.post("/chat", json={"user_id": "u_ana", "question": "marker-question-Zz42"})
    conversation_id = first.json()["conversation_id"]
    rejected = client.post("/chat", json={
        "user_id": "u_cora", "conversation_id": conversation_id, "question": "am I allowed?",
    })
    assert rejected.status_code == 409
    assert "error" in rejected.json()
    assert len(calls) == 1  # the rejected turn never reached the agent loop
    records = client.get(f"/audit/{conversation_id}", params={"user_id": "u_ana"}).json()
    assert len(records) == 1  # audit trail untouched by the rejected turn


def test_chat_unknown_user_404(client, monkeypatch):
    install_fake_runner(monkeypatch)
    response = client.post("/chat", json={"user_id": "u_ghost", "question": "hello"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_user"


def test_chat_missing_question_422(client, monkeypatch):
    install_fake_runner(monkeypatch)
    for question in (None, "", "   "):
        payload = {"user_id": "u_ana"}
        if question is not None:
            payload["question"] = question
        response = client.post("/chat", json=payload)
        assert response.status_code == 422, f"question={question!r}"
        assert response.json()["error"]["code"] == "missing_question"


def test_chat_unknown_conversation_404(client, monkeypatch):
    install_fake_runner(monkeypatch)
    response = client.post("/chat", json={
        "user_id": "u_ana", "conversation_id": "conv-not-a-real-id", "question": "hello",
    })
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_conversation"


def test_chat_ignores_client_supplied_role(client, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    response = client.post("/chat", json={
        "user_id": "u_ana", "question": "list national ids", "role": "compliance",
    })
    assert response.status_code == 200
    assert calls[0]["user_id"] == "u_ana"  # the smuggled role never reached the loop
    assert response.json()["audit"]["identity"]["role"] == "analyst"


def test_chat_malformed_body_422_structured(client):
    response = client.post("/chat", json={})
    assert response.status_code == 422
    body = response.text
    assert "SELECT" not in body and "Traceback" not in body
    assert "input" not in response.json()["error"]  # no request echo


def test_agent_error_surfaces_as_error_kind(client, monkeypatch):
    install_fake_runner(monkeypatch, answer="", error="APIError: simulated outage")
    response = client.post("/chat", json={"user_id": "u_ana", "question": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer_kind"] == "error"
    assert "APIError" not in json.dumps(body)  # no internal error text echoes out


# ---------------------------------------------------------------- chart extraction


def test_chart_extracted_when_produced(client, monkeypatch):
    install_fake_runner(monkeypatch, transcript=[CHART_LINE])
    response = client.post("/chat", json={"user_id": "u_ana", "question": "chart customers by segment"})
    assert response.status_code == 200
    chart = response.json()["chart"]
    assert chart["mark"] == "bar"
    assert chart["data"]["values"] == CHART_SPEC["data"]["values"]


def test_chart_absent_when_not_produced(client, monkeypatch):
    install_fake_runner(monkeypatch)
    response = client.post("/chat", json={"user_id": "u_ana", "question": "count customers"})
    assert "chart" not in response.json()


# ---------------------------------------------------------------- GET /audit scoping


def test_audit_requires_identity(client, monkeypatch):
    install_fake_runner(monkeypatch)
    conversation_id = client.post("/chat", json={"user_id": "u_ana", "question": "hi"}).json()["conversation_id"]
    missing = client.get(f"/audit/{conversation_id}")
    assert missing.status_code == 422
    unknown = client.get(f"/audit/{conversation_id}", params={"user_id": "u_ghost"})
    assert unknown.status_code == 404


def test_audit_nonexistent_conversation_404(client, monkeypatch):
    install_fake_runner(monkeypatch)
    response = client.get("/audit/conv-never-existed", params={"user_id": "u_ana"})
    assert response.status_code == 404


def test_audit_cross_identity_403(client, monkeypatch):
    install_fake_runner(monkeypatch)
    conversation_id = client.post("/chat", json={"user_id": "u_ana", "question": "hi"}).json()["conversation_id"]
    foreign = client.get(f"/audit/{conversation_id}", params={"user_id": "u_cora"})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "forbidden"


def test_audit_own_identity_returns_only_own_records(client, monkeypatch):
    install_fake_runner(monkeypatch)
    conversation_id = client.post("/chat", json={"user_id": "u_ana", "question": "hi"}).json()["conversation_id"]
    client.post("/chat", json={"user_id": "u_ana", "conversation_id": conversation_id, "question": "more"})
    response = client.get(f"/audit/{conversation_id}", params={"user_id": "u_ana"})
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 2
    assert all(r["identity"]["user_id"] == "u_ana" for r in records)
    assert all("u_cora" not in json.dumps(r) for r in records)
    assert all(r["resolved_scope"]["row_scope"] for r in records)


# ---------------------------------------------------------------- error bodies never echo


def test_error_bodies_never_echo_question_sql_or_traceback(client, monkeypatch):
    install_fake_runner(monkeypatch, answer="marker-answer-Zz42", sql_log=["SELECT secret FROM x"])
    first = client.post("/chat", json={"user_id": "u_ana", "question": "marker-question-Zz42"})
    conversation_id = first.json()["conversation_id"]
    probes = [
        client.post("/chat", json={"user_id": "u_cora", "conversation_id": conversation_id, "question": "q"}),
        client.post("/chat", json={"user_id": "u_ana", "conversation_id": "conv-nope", "question": "q"}),
        client.post("/chat", json={"user_id": "u_ghost", "question": "q"}),
        client.post("/chat", json={"user_id": "u_ana"}),
        client.get("/audit/conv-nope", params={"user_id": "u_ana"}),
        client.get(f"/audit/{conversation_id}", params={"user_id": "u_cora"}),
    ]
    for probe in probes:
        body = probe.text
        assert probe.status_code in (403, 404, 409, 422)
        assert "SELECT" not in body, body
        assert "Traceback" not in body, body
        assert "marker-question-Zz42" not in body, body
        assert "marker-answer-Zz42" not in body, body


# ---------------------------------------------------------------- GET /scope display


def test_scope_endpoint_returns_the_audit_resolution(client):
    response = client.get("/scope/u_ana")
    assert response.status_code == 200
    body = response.json()
    assert body == audit.resolved_scope(db.get_user("u_ana"))  # single-sourced with the audit record
    assert body["row_scope"] == "region='WEST' AND deleted_at IS NULL"
    assert body["column_tiers"] == ["T0", "T1"]


def test_scope_endpoint_all_roles_and_unknown_user(client):
    for user_id in ("u_ben", "u_cora", "u_fern", "u_ops", "u_rae"):
        assert client.get(f"/scope/{user_id}").status_code == 200, user_id
    assert client.get("/scope/u_ghost").status_code == 404


# ---------------------------------------------------------------- served assets and env


def test_static_index_served_and_clean(client):
    for path in ("/", "/index.html"):
        response = client.get(path)
        assert response.status_code == 200, path
        lowered = response.text.lower()
        assert "openai" not in lowered and "sk-" not in lowered and "select " not in lowered


def test_static_app_js_served_and_clean(client):
    """VAL-UI-015: the SPA's own code carries no key material and no enforcement logic —
    presentation only, every decision stays server-side."""
    response = client.get("/app.js")
    assert response.status_code == 200
    body = response.text
    assert "OPENAI" not in body
    assert "sk-" not in body
    assert "Authorization" not in body
    assert "api.openai" not in body


def test_env_loader_reads_file_only_when_unset(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nOPENAI_API_KEY="file-key"\nOTHER=1\n', encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    interface_app._load_env(str(env_file))
    assert os.environ["OPENAI_API_KEY"] == "file-key"
    monkeypatch.setenv("OPENAI_API_KEY", "env-wins")
    interface_app._load_env(str(env_file))
    assert os.environ["OPENAI_API_KEY"] == "env-wins"  # an existing environment value wins
