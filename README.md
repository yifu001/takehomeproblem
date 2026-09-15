# Take-home: role-aware conversational BI

**Time: 4-8 hours.** It is a cap, not a target.  Tell us where you stopped and why.

**Use AI tooling.** Claude Code, Cursor, whatever you normally use — we do. We ask for
your prompts and where you overrode the model (see Deliverables). That part is not a
formality; it is one of the things we read most carefully.

---

## What this is

`agent/` contains a working conversational-BI agent over a small fraud-operations
warehouse. Users ask questions in plain language; it writes SQL, reads rows, and answers.
It runs. It is also wrong in ways that matter.

Different users have different roles and are entitled to different data. **The core of
this exercise is where that entitlement is enforced.** Everything else is secondary, and
we have said so in the rubric below.

## Setup

```bash
pip install -r requirements.txt
python seed.py
export OPENAI_API_KEY=...          # 
python eval/run_eval.py --ids c2      # smoke test: one question, ~15s
python eval/run_eval.py               # full suite, ~36 agent runs
```

The key is yours for the exercise and has a fixed budget. Keep it out of anything you send
back.

The model is a one-line constant in `agent/baseline.py`; `gpt-5.5` is only a default.
Use whichever model you prefer — nothing here is tuned to a particular one, and if you
would rather point it at another provider with your own key, that is fine too.

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
