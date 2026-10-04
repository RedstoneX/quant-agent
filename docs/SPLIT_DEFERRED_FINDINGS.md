# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

## Owner-alert senders that bypass the retry funnel

`send_owner_alert` retries through `deliver_with_retry` and records an undelivered alert (fixed by #1104, witnessed by `tests/test_owner_alert_delivery.py`; replacing the funnel call with a bare send turns it red, re-measured 2026-10-04). What remains: code that builds its own `TelegramNotifier()` and sends escapes that retry entirely, and no guard forbids it.

Measured 2026-10-04 on main: six construction sites in `src/` outside the funnel (`alert_watchdog.py` default, `pipeline.py`, `scheduler.py`, `cost_circuit/alert_ledger.py`, two in `cost_circuit/breaker.py`) across five modules, not six; whether the `scheduler.py` one sends owner alerts is not checked. About ten more sites in `scripts/` also construct one and are not counted in the earlier sizing.

Fix: route each sender through the funnel (or justify and allow-list it), then add a guard that fails on any new direct construction. Sized at roughly 150-250 lines for the `src/` sites and the guard [estimate: earlier agent sizing, not re-derived]; the scripts would add to it.

## The cost circuit is eleven mixins, not eleven modules

It sits under the ceiling, but eleven of its nineteen pieces are mixin groups,
which cannot be built or exercised on their own. Smaller files, not
boundaries. Fix: convert them the way the sessions, exits, protection, broker
and storage packages were done, and add witness tests.

CLOSED 2026-10-02 (fourth and fifth instalments): all eleven parts are HELD
instances on `LLMCostCircuitBreaker` (`_hold_parts`, wired by `assembly.py`); every `breaker_*.py`
shim module is deleted and the class inherits from nothing. Three
collaborators had to be handed in live rather than snapshotted (`notifier`,
`_connect`, the owner-notify sentinel); see docs/ARCHITECTURE.md.

VERIFIED CLOSED 2026-10-04 (re-measured on main f85da78d, no code change needed): the gap the 2026-10-02 verdict left open is gone. `src/cost_circuit/parts/` holds eleven classes, each with no base class and keyword-only collaborators (admission 310, alert_formats 151, circuit_state 363, emergency_latch 260, episode_wording 223, infra_retry 280, operator_controls 275, owner_notify 344, quota_holds 346, session_lifecycle 228, settlement 399 lines -- all at or under the 400 ceiling). No `breaker_*.py` shim module exists on disk and no `Mixin` name survives anywhere under `src/cost_circuit/`. `tests/test_cost_circuit_parts_boundary.py` passes 38 tests (not 35): it CONSTRUCTS all eleven from stubs alone, EXERCISES each one with no breaker behind it, asserts `LLMCostCircuitBreaker.__mro__ == (LLMCostCircuitBreaker, object)`, asserts each former shim module now raises `ModuleNotFoundError` on import, and asserts the live-collaborator contract (a body swapped on a held part after construction is what runs -- the snapshotted-`getattr` defect). Nothing remains open in this section.

VERDICT 2026-10-02 (isolation pass): INCORRECT. Earlier verdict read only the `class LLMCostCircuitBreaker(...)` line; the parts ARE already standalone. `src/cost_circuit/parts/` has eleven standalone classes (merged #1064, #1066, #1070 on 2026-10-02); `tests/test_cost_circuit_parts_boundary.py` passes 35 tests, building all eleven from stubs with no breaker composition. The `breaker_*.py` files are thin per-call shims. ONE genuine gap remains: the breaker still INHERITS the shims rather than HOLDING part instances; that conversion changes every test patch target, so it is a separate deliberate instalment.

## Real boundaries still owed

The position builder, the portfolio-manager seat and the prompt-facts review
chunk are under the ceiling but are not separable pieces. Same treatment.

STILL OPEN, measured 2026-10-02: `tests/test_boundary_harness.py` passes 12
tests but covers only the pipeline mixins; mixins remain in
`src/pipeline_prompt_facts_review.py` and
`src/agents/portfolio_manager/prompt_evidence.py`.

DONE 2026-10-04 for `PromptEvidenceMixin`: `PortfolioManagerAgent` no longer inherits it. `hold_prompt_evidence` builds one `PromptEvidence` and installs classmethod delegates on the agent (61 class-level test call sites counted, all still resolving, none rewritten); `tests/test_portfolio_manager_parts_boundary.py` builds and runs the part on a bare class. Three mixins remain on the seat (`DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`) plus `PromptFactsReviewMixin`.

SIZED 2026-10-04, NOT STARTED (a run that closed the cost-circuit section above measured this one rather than half-doing it): four pieces remain and they are two different jobs. Three are the SAME conversion already proven on `PromptEvidenceMixin` -- `DecisionGroundingMixin`, `RotationSectionMixin` and `CandidateRankingMixin` each already have a standalone part behind them (`DecisionGrounding` in decision_grounding.py 873 lines, `RotationSection` in rotation_rendering.py 404, `CandidateRanking` in candidate_ranking.py 627), so the work is to stop the agent inheriting the shim and make it HOLD the part with same-named delegates, one instalment per mixin. The fourth, `PromptFactsReviewMixin` (`src/pipeline_prompt_facts_review.py`, 1309 lines, ONE class, no part extracted at all), is a different and much larger job: a pure-move extraction carved into at least four modules of 400 lines or fewer, each with its own construct-alone test. Natural pieces: one instalment per PM mixin (three), then the prompt-facts-review extraction as its own multi-part instalment. Do not attempt the four together.

VERDICT 2026-10-02 (isolation pass): REPRODUCED (structural debt). Mixins remain: `PromptFactsReviewMixin` (`src/pipeline_prompt_facts_review.py`) and in `src/agents/portfolio_manager/`: `DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`, `PromptEvidenceMixin`; `tests/test_boundary_harness.py` passes (with the other two files run, 20 passed) but only covers pipeline mixins. The note named three pieces; the portfolio-manager seat actually has four mixins. Rebuild size: medium-large, five pieces.

## Nothing in the build checks for undefined names

A bare `_log` in the take-profit refusal branches raised `NameError` in a cold path (fixed by #1102; `tests/test_take_profit_refusal_names.py` goes red on all three cases if a bare `_log.error` is put back, re-measured 2026-10-04; no sibling survives in `src/`). The class is not closed: there is no ruff, flake8 or pyflakes in `pyproject.toml` or `.github/workflows/`, and no script in `scripts/` resolves names, so the next one ships the same way.

Fix: a stdlib-only scope-analysing guard in `scripts/` plus one workflow edit, roughly 250-350 lines [estimate: earlier agent sizing, not re-derived], with its false-positive rate on the existing tree driven to zero first. A lint dependency is the alternative and needs an owner call as a new dependency.

---

# Resolved history

These findings have been addressed through code fixes or established as not defects. Preserved here for completeness and context.

### The midnight clock bug (CLOSED -- both halves, 2026-10-04)

Between 00:00 and roughly 00:16 Eastern, tests compare an exchange trading day
against the runner's local day and fail. Seven in the holding-discipline
intraday file, thirteen more in the cost circuit. They pass again at 00:20 ET.
Verified twice on 2026-10-02. This is the third or fourth instance of the same
class. It costs a full test round every time it fires and it reds every open
change at once, which is why it goes first.

DONE 2026-10-02: the holding-discipline half. Root cause was a module-level
`str(et_today())` stamped when the file is COLLECTED, compared against an
`et_today()` read when the test RUNS -- a suite that crosses ET midnight
between the two compares two different exchange days. Reproduced on demand by
shifting the clock (collect 23:58 ET, run 00:05 ET): 6 failed before the fix,
26 passed after, at the same simulated instant. The stamp is now read at run
time. A mechanical guard, `tests/test_no_local_day_as_exchange_day.py`, now
fails on `date.today()`, a naive `datetime.now()`, a UTC calendar day used as
a day, and an import-time clock stamp in a test, with a shrink-only baseline
of the offenders that already existed.

CLOSED 2026-10-04: the cost-circuit half. Its cause (Python ET day vs SQLite's own `'now'`) was already removed by the single clock in `src/cost_circuit/clock.py`; re-measured with the clock pinned to 00:01, 00:05 and 00:15 ET and with the real UTC day differing from the ET day: 0 failures in 180 tests. NOTE: The text claims `tests/test_cost_circuit_single_clock.py` as a guard against re-adding a second clock, but this test file does not exist on main (verified 2026-10-04). The measured result (0 failures in 180 tests) stands, but the guard implementation differs from what is documented. Earlier text kept below for history: the cost-circuit half. The original entry named thirteen failing tests but provides no test names. Reproduction attempts on 2026-10-02 failed by three methods: clock simulation with Python and SQLite moved together showed zero failures; CI runs covering 00:00-00:16 ET showed no cost-circuit test failures; and direct inspection identified no named failure. This entry must not be treated as a known defect until a specific failing test is named and reproduced.

VERDICT 2026-10-02 (isolation pass): holding-discipline half -- ALREADY FIXED (module-level stamp now read at run time; `tests/test_no_local_day_as_exchange_day.py` guard passes, no import-time `str(et_today())` stamp remains in that file). Cost-circuit half -- NOT REPRODUCED (carried forward from the earlier investigation; not redone). The note names no test, so it should not be treated as a known defect.

### A broker read error reads as "no stop to adjust" -- FIXED

The stop read used to return nothing on ANY error, and the protection path
read nothing as "no stop" and skipped. Now there are three answers (found,
none, unreadable) in `src/execution/stop_read.py`, and unreadable is
escalated (owner ruling 2026-10-02): two retries with a short pause, then the
broker's full open-orders list, then the protection paths (ex-dividend shift
and deterministic trail) re-place the stop through coverage repair, which is
idempotent because the order key has landed; a stop that really was there may
be duplicated, which the owner accepted. A durable row and an owner alert say
what the desk actually did (placed, tried and failed, or only reported).
Reporting-only reads (prompt, evening proximity, reconcile) place nothing. The
prompt states "stop could not be read, do NOT treat as unprotected". A
non-numeric broker answer is unreadable: the real read only returns a number or
nothing, so anything else is a broken answer. Proven through the ex-dividend
caller: every read failing places the stop once; failing twice then
succeeding places nothing. All seven callers use it; the watchdog reconcile now
passes the database it was already given.

VERDICT 2026-10-02 (isolation pass): ALREADY FIXED. Fixed by #1085 (c1fdebc8), `src/execution/stop_read.py`. Proof: `tests/test_stop_read_unknown.py` plus `tests/test_evidence_gate.py` pass (149 passed); a grep finds no caller reading the broker stop outside `read_stop`; seven call sites (pipeline_exits x2, pipeline, pipeline_prompt_facts x2, pipeline_protection, stop_records) use it. Not reverted-to-prove: the pre-fix source was not re-run.
