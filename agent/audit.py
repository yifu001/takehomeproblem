"""The per-turn audit record — the deliverable that answers, per turn, "what did the
agent see, and on whose authority?".

One JSONL line is appended to `audit/turns.jsonl` per agent turn (one `baseline.run`
call — an eval turn or a UI chat turn alike; both surfaces write the same format). The
policy engine (agent/policy.py) emits a structured ExecutionReport per statement; this
module aggregates those reports into the turn record and owns the JSONL writer, the
turn-id allocation, and the offline report/metrics CLI (`python -m agent.audit [path]`).

Record schema (one JSON object per line; the `reason` and `params` fields extend the
architecture §6 base schema, everything else is exactly §6):

    turn_id          str   "{conversation_id}:turn_{seq:03d}", globally unique (a
                           collision, e.g. after a process restart, gets a uuid suffix)
    timestamp_utc    str   ISO-8601 Zulu time of the turn's start
    identity         obj   user_id, role, region — server-resolved; region is
                           "(all regions)" for roles without a regional assignment
    resolved_scope   obj   column_tiers (tier names, [] for admin), row_scope (the
                           enforced predicate, human-readable), case_notes (bool),
                           aggregate_only (bool), tables_denied (tables with no read
                           access at all; users' own-row restriction is not a denial
                           and is not listed)
    question         str   the user's question, verbatim
    tool_calls       arr   one entry per tool invocation, in order:
      tool             str   tool name (run_sql, describe_table, ..., decline)
      sql_requested    str   the model's original SQL ("" for non-SQL tools)
      sql_executed     str   the REWRITTEN statement actually executed — never the
                             model's original; for floor-stage refusals, the rewritten
                             statement the floors stopped (not executed); "" when no
                             rewritten statement exists (earlier refusals, non-SQL tools)
      rewrites_applied arr   policy rewrites for this call (non-null; [] when none)
      refusal          obj   {category, detail} from the policy taxonomy, or null
      rows_returned    int   rows the statement returned (make_chart re-renders a result
                             already recorded under its run_sql call, so it reports 0)
      latency_ms       int   wall time of the call
      params           obj   server-side scope bindings (:scope_region, :scope_user_id)
                             sql_executed must be replayed with ({} when unparameterized)
      reason           str   the model's or loop's stated reason for decline calls, the
                             clarifying question for clarify calls, else null — the
                             policy category on the refused call plus the stated reason
                             on the decline call gives both refusal layers (§4.5)
    answer_kind      str   "answer" | "clarify" | "decline" | "error"
    redactions_applied arr  output suppressions applied to the turn (floor group/cell
                             suppression, row-cap truncation); [] when none
    tokens           obj   input/output prompt+completion token counts for the turn
    total_latency_ms int   wall time of the whole turn

Records are metadata only — never row values — so the audit file cannot itself become
a disclosure channel. The offline CLI (`python -m agent.audit [path]`) parses the file,
checks the schema, prints the per-identity seen/authority report and the p50/p95
latency, token, and dollar accounting (token counts x the documented pricing step).
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from . import floors, policy

# Documented pricing step for the dollar accounting (VAL-CORR-019): dollars per question
# = tokens.input/1e6 * input price + tokens.output/1e6 * output price. These are the
# gpt-5.6-terra unit prices this mission's accounting uses; see the NOTES.md cost section.
PRICE_INPUT_USD_PER_MTOK = 1.25
PRICE_OUTPUT_USD_PER_MTOK = 10.00

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_REPO_ROOT, "audit", "turns.jsonl")

_ALL_REGIONS = "(all regions)"

_LOCK = threading.Lock()
_TURN_COUNTERS: dict[str, int] = {}
_SEEN_BY_PATH: dict[str, set[str]] = {}

_REQUIRED_TURN_FIELDS = (
    "turn_id", "timestamp_utc", "identity", "resolved_scope", "question",
    "tool_calls", "answer_kind", "redactions_applied", "tokens", "total_latency_ms",
)
_REQUIRED_IDENTITY_FIELDS = ("user_id", "role", "region")
_REQUIRED_SCOPE_FIELDS = ("column_tiers", "row_scope", "case_notes", "aggregate_only", "tables_denied")
_REQUIRED_CALL_FIELDS = ("tool", "sql_requested", "sql_executed", "rewrites_applied", "rows_returned", "latency_ms")


# ---------------------------------------------------------------- scope resolution


def resolved_scope(user: dict) -> dict:
    """The access scope the policy engine enforces for this identity, as recordable data.

    Derived from the same policy constants the pipeline enforces, so the recorded
    authority can never drift from the enforced authority.
    """
    role = user["role"]
    region = user.get("region")
    if role == "admin":
        row_scope = "no customer rows"
        tables_denied = sorted(policy.CUSTOMER_DATA_TABLES)
    elif role in policy.REGION_SCOPED_ROLES and region:
        row_scope = f"region='{region}' AND deleted_at IS NULL"
        tables_denied = [] if role in policy.CASE_NOTES_ROLES else ["case_notes"]
    elif role in policy.REGION_SCOPED_ROLES:
        row_scope = "no region assignment; no customer rows served"
        tables_denied = [] if role in policy.CASE_NOTES_ROLES else ["case_notes"]
    elif role == "fair_lending":
        row_scope = "deleted_at IS NULL (all regions)"
        tables_denied = ["case_notes"]
    else:  # compliance: all regions, including offboarded customers
        row_scope = "all regions, including offboarded customers"
        tables_denied = []
    return {
        "column_tiers": sorted(policy.ROLE_CUSTOMER_TIERS[role]),
        "row_scope": row_scope,
        "case_notes": role in policy.CASE_NOTES_ROLES,
        "aggregate_only": role == "fair_lending",
        "tables_denied": tables_denied,
    }


def _stated_reason(name: str, args: dict | None) -> str | None:
    """The model's stated reason for a terminal call (decline reason / clarify question)."""
    if not args:
        return None
    if name == "decline":
        return str(args.get("reason", ""))
    if name == "ask_clarifying_question":
        return str(args.get("question", ""))
    return None


def tool_call_record(
    name: str,
    *,
    report: policy.ExecutionReport | None = None,
    refusal_category: str | None = None,
    refusal_detail: str | None = None,
    latency_ms: int = 0,
    args: dict | None = None,
) -> dict:
    """One per-tool-call audit entry.

    run_sql calls pass the policy engine's ExecutionReport (the source for sql_executed,
    rewrites, refusal, rows, latency, scope params). Every other tool passes its refusal
    category (when the result was a normalized access refusal) and its measured latency;
    its SQL fields are empty strings and it returns no rows.
    """
    reason = _stated_reason(name, args)
    if report is not None:
        return {
            "tool": name,
            "sql_requested": report.sql_requested,
            "sql_executed": report.sql_executed or "",
            "rewrites_applied": list(report.rewrites_applied),
            "refusal": (
                {"category": report.refusal.category, "detail": report.refusal.detail}
                if report.refusal is not None
                else None
            ),
            "rows_returned": len(report.rows),
            "latency_ms": report.latency_ms if report.latency_ms is not None else 0,
            "params": dict(report.params),
            "reason": reason,
        }
    return {
        "tool": name,
        "sql_requested": "",
        "sql_executed": "",
        "rewrites_applied": [],
        "refusal": (
            {"category": refusal_category, "detail": refusal_detail or ""}
            if refusal_category is not None
            else None
        ),
        "rows_returned": 0,
        "latency_ms": int(latency_ms),
        "params": {},
        "reason": reason,
    }


# ---------------------------------------------------------------- writer


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seen_turn_ids(path: str) -> set[str]:
    """Turn ids already in the file (loaded once per path), so ids stay unique across
    process restarts. Unparseable lines are skipped here; the schema check reports them."""
    seen = _SEEN_BY_PATH.get(path)
    if seen is None:
        seen = set()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        seen.add(json.loads(line)["turn_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
        _SEEN_BY_PATH[path] = seen
    return seen


def append_turn(record: dict, path: str | None = None) -> str:
    """Append one turn record as a JSONL line; returns the (uniquified) turn_id."""
    path = path or DEFAULT_PATH
    with _LOCK:
        seen = _seen_turn_ids(path)
        turn_id = record["turn_id"]
        if turn_id in seen:
            # A conversation id reused after a process restart would restart its turn
            # numbering; suffix instead of colliding — uniqueness is global (VAL-CROSS-016).
            turn_id = f"{turn_id}-{uuid.uuid4().hex[:8]}"
            record["turn_id"] = turn_id
        seen.add(turn_id)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return turn_id


def reset_runtime_state() -> None:
    """Clear per-process turn counters and seen-id caches (between test sessions)."""
    with _LOCK:
        _TURN_COUNTERS.clear()
        _SEEN_BY_PATH.clear()


class TurnRecorder:
    """Collects one agent turn's tool-call reports and writes the turn's audit record.

    The agent loop creates one recorder per `baseline.run` call, feeds it every tool
    outcome, and calls `finish` on every return path; `finish` builds the record,
    appends it to the JSONL file, and returns it (also carried as `AgentResult.audit`
    for the interface layer).
    """

    def __init__(self, question: str, user: dict, conversation_id: str | None = None, path: str | None = None) -> None:
        self.question = question
        self.user = user
        self.path = path  # None defers to DEFAULT_PATH at write time
        self.started = time.monotonic()
        self.timestamp = _utc_now_z()
        self.calls: list[dict] = []
        self._redactions: list[str] = []
        if conversation_id is None:
            # Eval-mode turns have no conversation; a generated id keeps turn_ids unique
            # across eval runs and surfaces.
            conversation_id = f"eval-{uuid.uuid4().hex[:12]}"
        self.conversation_id = conversation_id
        with _LOCK:
            seq = _TURN_COUNTERS.get(conversation_id, 0) + 1
            _TURN_COUNTERS[conversation_id] = seq
        self.turn_id = f"{conversation_id}:turn_{seq:03d}"

    def _note_redactions(self, report: policy.ExecutionReport) -> None:
        for note in report.notes:
            if note == floors.K_SUPPRESSION_NOTE and "k_anonymity_group_suppression" not in self._redactions:
                self._redactions.append("k_anonymity_group_suppression")
            if note == floors.T5_SUPPRESSION_NOTE and "protected_class_cell_suppression" not in self._redactions:
                self._redactions.append("protected_class_cell_suppression")
        if report.truncated and "row_cap_truncation" not in self._redactions:
            self._redactions.append("row_cap_truncation")

    def record_run_sql(self, report: policy.ExecutionReport) -> None:
        self.calls.append(tool_call_record("run_sql", report=report))
        self._note_redactions(report)

    def record_tool(
        self,
        name: str,
        args: dict,
        *,
        refusal_category: str | None = None,
        refusal_detail: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        self.calls.append(
            tool_call_record(
                name,
                refusal_category=refusal_category,
                refusal_detail=refusal_detail,
                latency_ms=latency_ms,
                args=args,
            )
        )

    def record_forced_decline(self, reason: str) -> None:
        """The loop-synthesized structured decline (policy-forced or prose backstop)."""
        self.calls.append(tool_call_record("decline", args={"reason": reason}, latency_ms=0))

    def answer_kind(self, result) -> str:
        if result.declined:
            return "decline"
        if result.clarified:
            return "clarify"
        if result.error:
            return "error"
        return "answer" if result.answer.strip() else "error"

    def finish(self, result) -> dict:
        region = self.user.get("region") or _ALL_REGIONS
        record = {
            "turn_id": self.turn_id,
            "timestamp_utc": self.timestamp,
            "identity": {
                "user_id": self.user["user_id"],
                "role": self.user["role"],
                "region": region,
            },
            "resolved_scope": resolved_scope(self.user),
            "question": self.question,
            "tool_calls": self.calls,
            "answer_kind": self.answer_kind(result),
            "redactions_applied": list(self._redactions),
            "tokens": {"input": result.input_tokens, "output": result.output_tokens},
            "total_latency_ms": int((time.monotonic() - self.started) * 1000),
        }
        append_turn(record, self.path)
        return record


# ---------------------------------------------------------------- offline report / metrics


def load_records(path: str | None = None) -> list[dict]:
    """Parse the whole JSONL file; raises ValueError on any unparseable line."""
    path = path or DEFAULT_PATH
    records: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: unparseable JSONL line ({exc})") from exc
    return records


def check_records(records: list[dict]) -> list[str]:
    """Schema check: every required field present and non-null on every record and
    tool-call entry (empty arrays are valid; refusals may be null); turn_ids unique."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        turn_id = record.get("turn_id") if isinstance(record, dict) else None
        where = f"record {index} ({turn_id or '?'})"
        if not isinstance(record, dict):
            problems.append(f"{where}: not a JSON object")
            continue
        if turn_id in seen_ids:
            problems.append(f"{where}: duplicate turn_id")
        seen_ids.add(turn_id)
        for field in _REQUIRED_TURN_FIELDS:
            if record.get(field) is None:
                problems.append(f"{where}: missing/null {field}")
        identity = record.get("identity") or {}
        for field in _REQUIRED_IDENTITY_FIELDS:
            if identity.get(field) is None:
                problems.append(f"{where}: identity.{field} is null")
        scope = record.get("resolved_scope") or {}
        for field in _REQUIRED_SCOPE_FIELDS:
            if scope.get(field) is None:
                problems.append(f"{where}: resolved_scope.{field} is null")
        tokens = record.get("tokens") or {}
        for field in ("input", "output"):
            if tokens.get(field) is None:
                problems.append(f"{where}: tokens.{field} is null")
        calls = record.get("tool_calls")
        if not isinstance(calls, list):
            problems.append(f"{where}: tool_calls is not an array")
            continue
        for cindex, call in enumerate(calls):
            cwhere = f"{where} tool_calls[{cindex}]"
            if not isinstance(call, dict):
                problems.append(f"{cwhere}: not a JSON object")
                continue
            for field in _REQUIRED_CALL_FIELDS:
                if call.get(field) is None:
                    problems.append(f"{cwhere}: missing/null {field}")
            if not isinstance(call.get("rewrites_applied"), list):
                problems.append(f"{cwhere}: rewrites_applied must be an array")
            refusal = call.get("refusal")
            if refusal is not None and (
                not isinstance(refusal, dict)
                or refusal.get("category") is None
                or refusal.get("detail") is None
            ):
                problems.append(f"{cwhere}: refusal must be {{category, detail}} or null")
    return problems


def per_identity_report(records: list[dict]) -> dict:
    """The one-liner demo's answer: per identity, what the agent saw (questions, tool
    calls, executed SQL) and on whose authority (role, region, resolved scope)."""
    report: dict[str, dict] = {}
    for record in records:
        user_id = record["identity"]["user_id"]
        entry = report.setdefault(
            user_id,
            {
                "user_id": user_id,
                "role": record["identity"]["role"],
                "region": record["identity"]["region"],
                "resolved_scope": record["resolved_scope"],
                "turns": 0,
                "questions": [],
                "tool_calls": [],
                "sql_executed": [],
                "refusals": [],
            },
        )
        entry["turns"] += 1
        entry["questions"].append(record["question"])
        for call in record["tool_calls"]:
            entry["tool_calls"].append(call["tool"])
            if call["sql_executed"]:
                entry["sql_executed"].append(call["sql_executed"])
            if call["refusal"]:
                entry["refusals"].append(call["refusal"]["category"])
    return report


def _percentile(sorted_values: list[float], pct: int) -> float:
    """Nearest-rank percentile over pre-sorted values."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _dollars(tokens_in: int, tokens_out: int) -> float:
    return (
        tokens_in * PRICE_INPUT_USD_PER_MTOK + tokens_out * PRICE_OUTPUT_USD_PER_MTOK
    ) / 1_000_000


def metrics(records: list[dict]) -> dict:
    """p50/p95 latency (seconds), token counts, and dollar costs per question — all
    computed only from the record fields the JSONL carries."""
    latencies = sorted(record["total_latency_ms"] / 1000 for record in records)
    per_question_tokens = sorted(
        record["tokens"]["input"] + record["tokens"]["output"] for record in records
    )
    per_question_dollars = sorted(
        _dollars(record["tokens"]["input"], record["tokens"]["output"]) for record in records
    )
    return {
        "questions": len(records),
        "latency_seconds_p50": _percentile(latencies, 50),
        "latency_seconds_p95": _percentile(latencies, 95),
        "tokens_input_total": sum(record["tokens"]["input"] for record in records),
        "tokens_output_total": sum(record["tokens"]["output"] for record in records),
        "tokens_per_question_p50": _percentile([float(t) for t in per_question_tokens], 50),
        "tokens_per_question_p95": _percentile([float(t) for t in per_question_tokens], 95),
        "dollars_per_question_p50": _percentile(per_question_dollars, 50),
        "dollars_per_question_p95": _percentile(per_question_dollars, 95),
        "dollars_total": sum(per_question_dollars),
        "price_input_usd_per_mtok": PRICE_INPUT_USD_PER_MTOK,
        "price_output_usd_per_mtok": PRICE_OUTPUT_USD_PER_MTOK,
    }


def records_for_conversation(conversation_id: str, path: str | None = None) -> list[dict]:
    """All records for one conversation, in turn order (the interface layer's
    GET /audit/{conversation_id} reads through this)."""
    prefix = f"{conversation_id}:turn_"
    return [
        record
        for record in load_records(path)
        if str(record.get("turn_id", "")).startswith(prefix)
        or str(record.get("turn_id", "")).startswith(f"{prefix[:-1]}-")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit-record report: per identity, what the agent saw and on whose "
            "authority, plus p50/p95 latency, token, and dollar accounting."
        )
    )
    parser.add_argument("path", nargs="?", default=None, help="audit turns.jsonl path (default: audit/turns.jsonl)")
    args = parser.parse_args(argv)

    path = args.path or DEFAULT_PATH
    if not os.path.exists(path):
        print(f"no audit file at {path}")
        return 1
    try:
        records = load_records(path)
    except ValueError as exc:
        print(f"FAIL {exc}")
        return 1

    # Schema first: an incomplete record must fail the file before any report is built.
    problems = check_records(records)
    if problems:
        print(f"FAIL: {len(problems)} schema problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"audit file: {path} ({len(records)} turns)")
    for entry in per_identity_report(records).values():
        scope = entry["resolved_scope"]
        print(f"\n{entry['user_id']} — role {entry['role']}, region {entry['region']}")
        print(f"  authority: tiers={scope['column_tiers']} row_scope={scope['row_scope']}")
        print(
            f"  case_notes={scope['case_notes']} aggregate_only={scope['aggregate_only']} "
            f"tables_denied={scope['tables_denied']}"
        )
        print(f"  saw {entry['turns']} turn(s)")
        for question in entry["questions"]:
            print(f"    asked: {question}")
        for sql in entry["sql_executed"]:
            print(f"    executed: {sql[:120]}{'...' if len(sql) > 120 else ''}")
        if entry["refusals"]:
            print(f"  refusals: {', '.join(entry['refusals'])}")

    m = metrics(records)
    print(f"\nmetrics over {m['questions']} question(s):")
    print(f"  latency p50/p95: {m['latency_seconds_p50']:.2f}s / {m['latency_seconds_p95']:.2f}s")
    print(
        f"  tokens: {m['tokens_input_total']} in / {m['tokens_output_total']} out "
        f"(p50/p95 per question: {m['tokens_per_question_p50']:.0f} / {m['tokens_per_question_p95']:.0f})"
    )
    print(
        f"  dollars: p50/p95 ${m['dollars_per_question_p50']:.4f} / ${m['dollars_per_question_p95']:.4f}, "
        f"total ${m['dollars_total']:.4f}"
    )
    print(
        f"  pricing step: ${m['price_input_usd_per_mtok']}/MTok input, "
        f"${m['price_output_usd_per_mtok']}/MTok output"
    )
    print("\nschema check: OK (all required fields present and non-null, turn_ids unique)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
