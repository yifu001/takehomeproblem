# Take-home: role-aware conversational BI

**Time: 4-8 hours.** It is a cap, not a target.  Tell us where you stopped and why.

**Use AI tooling.** Claude Code, Cursor, whatever you normally use — we do. We ask for
your prompts and where you overrode the model (see Deliverables). That part is not a
formality; it is one of the things we read most carefully.

---

## What this is

`agent/` contains a conversational-BI agent over a small fraud-operations
warehouse. Users ask questions in plain language; it writes SQL, reads rows, and answers.

The version that ships here is the locked-down one. Enforcement is structural and
lives server-side, in a policy pipeline every statement passes through before its
results can reach the model's context. The baseline's enforcement — a model-supplied
`role` parameter plus a string-match on the SELECT list — is gone; what replaced it
is summarized under [Architecture](#architecture-summary) and in detail in `NOTES.md`.
Prompt injections are planted in the data (they are part of the fixture); the agent
ignores them, and the eval proves it.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python seed.py            # builds fraud.db; idempotent, deterministic
export OPENAI_API_KEY=...           # your key
.venv/bin/python eval/run_eval.py --ids c2      # smoke test: one question, ~15s
.venv/bin/python eval/run_eval.py               # full suite, ~52 agent runs
```

Notes:

- Python 3.14 was used; nothing exotic is pinned. All dependencies are in
  `requirements.txt`.
- `OPENAI_API_KEY` can also live in a `./.env` file at the repo root instead of the
  environment. The interface loads it from there automatically when the variable is
  not already exported; the eval CLI reads the exported variable. Either way, the
  key never enters git — `.env` is gitignored, and `git ls-files` shows no trace of
  it. Keep it out of anything you send back.
- `seed.py` is the schema and the fixture in one file and may be re-run freely;
  it rebuilds the same database byte-for-byte.

## Running the eval

```bash
.venv/bin/python eval/run_eval.py                # everything (~52 runs, a few minutes)
.venv/bin/python eval/run_eval.py --only leaks   # the binary gate; must print CLEAN
.venv/bin/python eval/run_eval.py --ids c2,L6    # targeted cases while iterating
.venv/bin/python eval/run_eval.py --verbose      # print transcripts for failures
```

`--only leaks` is the submission gate: a leak is restricted data entering the model's
context anywhere in a transcript, and one leak is a failing submission. The suite is
52 cases: 24 correctness + calibration (c1-c12, t1-t3, b1-b4, u1-u3, d1-d2) and 28
data-access (L1-L28, including the 11 we added). Each run is a real model call
(~8s, ~3.2K tokens each); see `NOTES.md` for the cost accounting.

## Running the interface

```bash
.venv/bin/python -m uvicorn interface.app:app --host 127.0.0.1 --port 3123
```

Then open http://localhost:3123. A ~3-minute recording script with the exact prompts
to type and what should appear is in `WALKTHROUGH.md`.

## Testing

```bash
.venv/bin/python -m pytest                    # unit suite; no API access needed
.venv/bin/python -m compileall agent interface
```

The pytest suite (367 tests) covers the policy engine against hostile SQL
(subqueries, CTEs, UNION, window functions, comments, multi-statements, PRAGMA,
casts), the disclosure floors, join-cascade scoping, and cache/history identity
binding. It runs offline.

## Architecture summary

One-line design: **the model never holds power; every tool result is produced by a
policy pipeline that runs before data can reach the model's context.**

```
User (identity picked in UI) ──► FastAPI backend (port 3123)
                                    │ resolves identity server-side via db.get_user()
                                    ▼
                              Agent loop (agent/baseline.py)
                                    │ tools: run_sql, make_chart, list/describe,
                                    │        ask_clarifying_question, decline — NO role param
                                    ▼
                              Policy engine (agent/policy.py)
   parse (SQLite, single SELECT) → qualify (expand *, resolve aliases/CTEs)
   → column-tier authorization over the whole statement
   → generalization substitution (zip_code→zip3, dob→birth_year, income→income_band)
   → row-scope rewrite (every customers reference wrapped; cascades through joins)
   → floors (k=2; T5 reporting rule) → execute (read-only conn, authorizer,
     row cap, timeout) → audit record
                                    ▼
                              audit/turns.jsonl  ·  Chat UI (vanilla JS + Vega-Lite)
```

The load-bearing decisions:

- **Identity is server-side, per turn.** The `role` parameter no longer exists on
  any tool; role claims in tool arguments are stripped. A conversation is bound to
  its first identity (mismatch is a 409), history is identity-bound, and the query
  cache is keyed by (identity, post-policy SQL).
- **Only rewritten SQL executes.** The model's original string is never run; this
  neutralizes parser-differential tricks. Forbidden columns are refused anywhere in
  the statement — SELECT list, WHERE, JOIN-ON, GROUP BY, ORDER BY, window specs,
  CTE bodies, subqueries — not merely hidden from output.
- **Row scope cascades through joins.** Transactions, alerts and case_notes have no
  region column; the rewrite wraps every `customers` reference so the scope reaches
  them through the join.
- **Generalization, not denial, for the analyst tier:** a zip ask returns zip3, an
  age ask returns birth_year, an income ask returns income_band. The floors run at
  the result layer: k=2 on customer-grain aggregates, and the T5 reporting rule
  (population ≥ 10, every cell ≥ 3, suppression-derivation check).
- **Fail closed, and say why.** Every access refusal is a structured decline that
  states access as the reason and carries a machine-readable category
  (`column_denied`, `row_scope`, `table_denied`, `statement_kind`, `parse`,
  `floor_k_anonymity`, `floor_protected_class`). Refusals are visually distinct
  from empty results everywhere, including on screen.
- **Every turn writes an audit record** to `audit/turns.jsonl`: identity, resolved
  scope, requested vs executed SQL, rows, refusals with categories, redactions,
  tokens, latency. Metadata only, never row values. `NOTES.md` shows how to query it.

Module map: `agent/policy.py` (permission matrix, pipeline, floors, execution), 
`agent/audit.py` (audit schema + JSONL), `agent/baseline.py` (agent loop), 
`agent/tools.py` (tool dispatch, error contract, handle-based charts), 
`agent/db.py` (read-only connection factory), `interface/` (FastAPI + SPA),
`eval/` (harness + case suites), `tests/` (pytest).

---

The rest of this document is the original assignment: the data, the permission model
(the specification the implementation follows), the task as given, the interface,
and the deliverables and scoring rubric.

## The data

Five tables — `users`, `customers`, `transactions`, `alerts`, `case_notes`. Read
`seed.py`; it is the schema and the fixture in one file. A few things worth knowing up
front, because they are properties of the domain rather than puzzles:

- `transactions.amount_minor` is in **minor units** (cents, centavos).
- All timestamps are **UTC**.
- `customers.deleted_at` is a **soft delete**. Offboarded customers are not current
  customers.
- More than one currency is present and there is **no FX table**.

Everything else in the data, you should discover.

## The permission model

This is the specification. Implement it exactly; do not infer a hierarchy that isn't
written here. **The columns are tiered, and the tiers are not a ladder.**

| Tier | Columns |
|---|---|
| **T0** open | `customer_id`, `full_name`, `region`, `segment`, `risk_score`, `onboarded_at`, `deleted_at` |
| **T1** generalized | `zip3`, `birth_year`, `income_band` |
| **T2** precise quasi-identifier | `zip_code`, `dob` |
| **T3** financial detail | `annual_income_usd` |
| **T4** direct identifier | `national_id`, `email`, `phone` |
| **T5** protected class | `race`, `ethnicity`, `sex` |

| Role | Rows | T0 | T1 | T2 | T3 | T4 | T5 | `case_notes` |
|---|---|---|---|---|---|---|---|---|
| `analyst` | own region, active only | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `reviewer` | own region, active only | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ |
| `compliance` | all regions, incl. offboarded | ✓ | ✓ | ✓ | ✓ | ✓ | **✗** | ✓ |
| `fair_lending` | all regions, active, **aggregate only** | region & segment only | ✓ | ✗ | ✓ | ✗ | ✓ | ✗ |
| `admin` | **none** — schema, `users`, system state | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

Three things in that table are deliberate and worth reading twice.

**T1 is a generalization, not a denial.** An analyst asking for a customer's zip should
get `zip3`; asking for age should get `birth_year`; asking about income should get
`income_band`. Returning a coarser value is a different behaviour from refusing, and both
are correct in different places.

**Protected class is purpose-limited, not privilege-gated.** `race`, `ethnicity` and `sex`
are collected for fair-lending monitoring, and may be used for nothing else. `compliance`
outranks every other role on every other column and still cannot touch T5 — their purpose
is investigation. `fair_lending` is the only role that can, and it is simultaneously the
*only* role that may never see a `customer_id` or a `full_name`. Any implementation that
ranks roles on a single axis cannot express this table.

**`admin` is an operations account.** Schema, `users`, and system state. Never a customer
record, never a transaction row. Not a superuser.

### Two disclosure floors

**Customer aggregates (k = 2).** An aggregate over `customers` is refused when it derives
from fewer than 2 distinct customers — a count or average resolving to one person
identifies that person. Applies to customer-attribute aggregates, not to transaction or
alert totals, which are business metrics.

**Protected-class aggregates (fair-lending reporting rule).** A T5 breakdown may be
reported only when the population being broken down is at least 10 customers **and** every
reported cell has at least 3. Cells below 3 are suppressed rather than reported — but if
suppression still allows a suppressed cell to be derived (because too few cells were
suppressed, or the attribute has only two categories and the total is known), the whole
breakdown must be refused instead.

*k* = 2 is low because the fixture is small; production would use 5–20. The mechanism is
what is being tested, not the constant.

**Refusing is not the same as returning nothing.** A user who is told "0 customers match"
when the real answer is "you may not see that region" has been given a false answer, and
will act on it. Refusals should say that access is the reason.

## What a leak means

**A leak is restricted data entering the model's context — not merely appearing in the
final answer.** If a tool returns rows the user is not entitled to and the model then
politely omits them, that is a leak. The model is not a trust boundary: text it reads can
carry instructions, its output is not guaranteed, and everything it sees lands in your
logs and traces.

`eval/run_eval.py` enforces this by scanning the entire transcript — every tool result and
every argument passed to `make_chart` — for values the acting user is not entitled to.

## Your task

1. **Find and fix the ways this agent discloses data it shouldn't.** `eval/leaks.yaml` has
   seventeen cases. They are not exhaustive; we planted more problems than we test for,
   and finding an untested one is worth more than fixing a tested one.
2. **Do not break correctness while locking it down.** Over-filtering is the easy wrong
   answer: an agent that reports a confidently wrong total is worse than one that leaks,
   because nobody can tell. `eval/questions.yaml` is the counterweight. It does not start
   clean, so not every failure there is one you caused. Several of its answers are not what
   a first-pass query returns.
3. **Extend the eval.** Add the cases you think are missing. We will read what you added
   more closely than what you fixed.
4. **Give it a face.** Right now this is a library with a CLI harness; there is no way to
   sit with it. Build a chat interface — pick an identity, ask a question, ask a follow-up,
   and see what the agent did on your behalf. A user should be able to ask for a chart or
   graph, and the interface should render it. What belongs on that screen is a judgement
   call.

## The interface

```bash
python -m uvicorn interface.app:app --host 127.0.0.1 --port 3123   # serves the SPA and the API
```

Then open http://localhost:3123. Vanilla JS + locally vendored Vega-Lite (`interface/static/vendor/`), no build step, no CDN at runtime.

**What is on the screen, on purpose:** the acting identity with its role badge and the server-resolved access scope (`GET /scope/{user_id}`, the same mapping every audit record carries); the message thread with multi-turn follow-ups; inline Vega-Lite charts rendered from the turn's authorized result handle; a collapsed-by-default transparency panel per assistant message (tool calls, the post-rewrite SQL that actually executed, rewrites applied, refusals with their category, and the audit record's scope fields); and three visually distinct non-answer states — a red-bordered **Access denied** card carrying the audit record's refusal category, a neutral gray **No matching rows** state for in-scope zero-row results, and a blue interactive **clarify card** whose inline reply continues the same conversation. Agent failures (including a conversation id left stale by a service restart) render an amber error card with a recovery action, never a silent empty answer.

**What is deliberately left off:** raw model transcripts, token counts and dollar costs, and any affordance to "fix" or override a refusal. A refusal is an answer; the screen says what the agent did (tool calls, executed SQL) but not what the model internally said.

**Identity switching:** switching returns to the picker; the previous conversation is parked in memory and resumes only when that same identity is picked again. Every conversation is bound to its first identity server-side — posting to it under a different identity is a 409 — so cross-identity content cannot appear in either direction. Nothing persists across a page reload: a reload always starts at the picker with an empty thread.

## Deliverables

A branch or patch, plus a `NOTES.md` of about two pages:

1. **Scorecard.** The output of `python eval/run_eval.py`, and one honest paragraph on
   what your number does *not* cover.
2. **What you found**, including anything the eval doesn't test — and anything you
   believe is unfixable in the current design, with the reason.
3. **What you deliberately did not do**, and why. This section carries real weight.
4. **The audit record.** Emit, per turn, the identity, the resolved access scope, the SQL
   actually executed, rows returned, and redactions applied. A bank's regulator asks
   "what did the agent see, on whose authority?" — show us where that answer comes from.
5. **Cost and latency.** p50 and p95 seconds per question, tokens and dollars per
   question, and one paragraph on what breaks at 100× the questions and 100× the rows.
6. **AI log.** The prompts you used, the places the model was wrong, and what you
   overrode.
7. **The interface.** How to run it, what you chose to put on the screen, and what you
   left off. The stack is yours — reach for whatever you would actually reach for, charting
   library included.
8. **A walkthrough.** A brief screen recording showing the implementation working and also summarizing the solution design and architecture

## How we score it

Published so you can aim at it rather than guess.

| Weight | |
|---|---|
| **gate** | `eval/run_eval.py --only leaks` is clean. Zero points; you cannot pass without it. |
| **25** | Where enforcement lives, and whether it survives an attacker-controlled input |
| **10** | The leak classes you found that we don't test for — and the ones you correctly call unfixable |
| **15** | Correctness held up under lockdown; the eval cases you added |
| **5** | Tool and error contracts — what a tool accepts, what it returns on refusal |
| **30** | The interface: what you chose to show, especially how a result is presented (graphs, tables, charts etc..), what happens to context when the identity changes, and whether a refusal reads differently from an empty result |
| **5** | The audit record |
| **10** | `NOTES.md`: what you cut, what your number doesn't cover, cost at 100× |

We are not scoring lines of code or test count. A framework on the agent has to earn its
place; on the interface, use whatever you like.
