"""The FastAPI chat backend (port 3123) — the only process this mission serves.

The interface is a thin, trusted layer over the agent loop: it resolves the acting
identity server-side on every turn (db.get_user), enforces conversation-identity
binding, and relays the turn's outcome. No enforcement logic lives here — the policy
engine (agent/policy.py) owns every access decision; nothing in this module inspects
SQL or data.

HTTP contract (architecture §7):

  GET /api/health
      {"status": "ok"} — service liveness.

  GET /users
      The 6 seeded identities, exactly the users-table fields the fixture stores
      (user_id, full_name, role, region). Public by design: the UI picker needs it
      before any identity is chosen.

  POST /chat  {user_id, question, conversation_id?}
      Runs one blocking agent turn (sync endpoint — FastAPI executes it in the
      threadpool) and returns:
        {conversation_id, answer, answer_kind, tool_calls, sql_executed, audit, chart?}
      - conversation_id omitted or null: a NEW conversation is created server-side
        and its id is returned; clients MUST send that id on follow-up turns.
      - conversation_id provided: it must exist (unknown -> 404) and belong to the
        requesting identity (mismatch -> 409) — switching identity therefore means
        starting a new conversation.
      - a client-supplied "role" field is ignored: identity is whatever db.get_user
        resolves, never anything from the request.
      - sql_executed lists only post-rewrite statements (what actually ran);
        chart carries the Vega-Lite spec when the turn produced one.
      - an agent-loop failure (e.g. the model API being down) is a normal turn
        outcome: 200 with answer_kind "error", never a stack trace.

  GET /audit/{conversation_id}?user_id=...
      The conversation's per-turn audit records (agent/audit.py — metadata only,
      never row values). user_id is required for scoping: unknown user -> 404,
      nonexistent conversation -> 404, another identity's conversation -> 403.

Every error body is structured JSON ({error: {code, message}}) that never echoes
SQL, warehouse data, or stack traces. Conversation bindings live in-process, so the
service runs a single worker; a restart makes old conversation ids unknown (404),
which the UI treats as a recoverable stale-conversation state.
"""

import json
import os
import threading
import uuid

from agent import audit, baseline, db
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_REPO_ROOT, ".env")
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Where GET /audit reads per-turn records; tests redirect this to a temp file.
AUDIT_PATH: str = audit.DEFAULT_PATH

app = FastAPI(title="fraud-ops chat", docs_url=None, redoc_url=None)

# conversation_id -> the user_id it is bound to. In-process by design; the agent
# loop's own history (baseline.conversation_owner) and the audit records are the
# backstops that keep a binding enforceable even if this registry loses an entry.
_CONVERSATIONS: dict[str, str] = {}
_CONV_LOCK = threading.Lock()


def _load_env(path: str = _ENV_PATH) -> None:
    """Load OPENAI_API_KEY from .env when the environment does not already carry it.

    The model key is a deployment secret, not configuration: an exported value wins,
    the file is read only as a fallback, and the value is never logged or returned.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "OPENAI_API_KEY":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        os.environ["OPENAI_API_KEY"] = value
                    return
    except OSError:
        return


_load_env()


# Sentinel for audit records that disagree on a conversation's owner — fail closed.
_AMBIGUOUS = object()


class ChatRequest(BaseModel):
    user_id: str
    # Optional here so an absent/null question reaches the handler below and gets the
    # uniform missing_question 422 instead of a framework-shaped validation body.
    question: str | None = None
    conversation_id: str | None = None


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Uniform 422 body: no field details, no echo of anything the request carried."""
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_request", "message": "request failed validation"}},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Every HTTP error renders as {error: {code, message}} — one uniform shape."""
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: a structured 500 with no internal detail of any kind."""
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "internal server error"}},
    )


def _resolve_user(user_id: str) -> dict:
    """The server-side identity resolution — the only source of role and region."""
    try:
        return db.get_user(user_id)
    except RuntimeError:
        # Unknown user (or unreadable warehouse): fail closed without echoing the id.
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_user", "message": "unknown user_id"},
        )


def _conversation_owner(conversation_id: str) -> str | None:
    """The identity a conversation is bound to, or None if the server never saw it.

    Consulted in order: the interface registry, the agent loop's history, the audit
    records (which survive restarts, so a binding stays enforceable across them).
    """
    with _CONV_LOCK:
        registered = _CONVERSATIONS.get(conversation_id)
    if registered is not None:
        return registered
    owner = baseline.conversation_owner(conversation_id)
    if owner is not None:
        return owner
    try:
        records = audit.records_for_conversation(conversation_id, path=AUDIT_PATH)
    except (ValueError, OSError):
        return None
    owners = {record["identity"]["user_id"] for record in records}
    return owners.pop() if len(owners) == 1 else (None if not owners else _AMBIGUOUS)


def _chart_from_transcript(result: baseline.AgentResult) -> dict | None:
    """The turn's rendered Vega-Lite spec, if make_chart produced one.

    make_chart serializes the spec into its tool result ("chart rendered: {json}");
    the interface lifts the last successful render so the UI can draw it without
    touching the agent's handle registry.
    """
    prefix = "[tool_result make_chart] chart rendered: "
    for line in reversed(result.transcript):
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):])
            except json.JSONDecodeError:
                return None
    return None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/users")
def list_users() -> list[dict]:
    """The seeded identities, exactly as stored (no other fields exist or are added)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT user_id, full_name, role, region FROM users ORDER BY user_id"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/chat")
def chat(body: ChatRequest) -> dict:
    user_id = body.user_id.strip()
    if not user_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_user", "message": "the 'user_id' field is required"},
        )
    question = body.question
    if not question or not question.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_question", "message": "the 'question' field is required"},
        )
    user = _resolve_user(user_id)  # server-side identity resolution, before anything else

    conversation_id = (body.conversation_id or "").strip()
    if not conversation_id:
        conversation_id = "conv-" + uuid.uuid4().hex
        with _CONV_LOCK:
            _CONVERSATIONS[conversation_id] = user_id
    else:
        owner = _conversation_owner(conversation_id)
        if owner is _AMBIGUOUS or (owner is not None and owner != user_id):
            # The conversation belongs to a different identity. Reject before the agent
            # loop runs: no answer, no history, no audit record for the rejected turn.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "identity_mismatch",
                    "message": "this conversation belongs to a different identity; "
                    "start a new conversation to switch identities",
                },
            )
        if owner is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "unknown_conversation", "message": "unknown conversation_id"},
            )
        with _CONV_LOCK:
            _CONVERSATIONS.setdefault(conversation_id, user_id)

    result = baseline.run(question.strip(), user_id, conversation_id)
    response: dict = {
        "conversation_id": conversation_id,
        "answer": result.answer,
        "answer_kind": result.audit["answer_kind"],
        "tool_calls": result.tool_calls,
        "sql_executed": list(result.sql_log),
        "audit": result.audit,
    }
    chart = _chart_from_transcript(result)
    if chart is not None:
        response["chart"] = chart
    return response


@app.get("/audit/{conversation_id}")
def get_audit(conversation_id: str, user_id: str) -> list[dict]:
    """Per-turn audit records for one conversation, scoped to the requesting identity."""
    _resolve_user(user_id)
    owner = _conversation_owner(conversation_id)
    try:
        records = audit.records_for_conversation(conversation_id, path=AUDIT_PATH)
    except (ValueError, OSError):
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_conversation", "message": "unknown conversation_id"},
        )
    if owner is None and not records:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_conversation", "message": "unknown conversation_id"},
        )
    if owner is _AMBIGUOUS or (owner is not None and owner != user_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "this conversation's audit trail belongs to a different identity",
            },
        )
    for record in records:  # fail closed per record: never serve a mixed-identity trail
        if record.get("identity", {}).get("user_id") != user_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "forbidden",
                    "message": "this conversation's audit trail belongs to a different identity",
                },
            )
    return records


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
