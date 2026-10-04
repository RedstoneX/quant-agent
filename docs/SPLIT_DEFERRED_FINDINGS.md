# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

## Owner alerts are sent and the result thrown away

Nearly every caller discards the return value of the owner-alert send, so a
failed delivery is indistinguishable from a successful one. Fix: make the
callers honour the result, and prove a failed send is visible somewhere.

VERDICT 2026-10-02 (isolation pass): NOT A DEFECT as written -- the note's consequence is false. 22 of 34 `send_owner_alert` call sites do discard the return, but a failed delivery is NOT invisible: `send_owner_alert` logs CRITICAL before sending, and the transport records every attempt in `notifier_sends` with status `failed` and the reason. Reproduced with the real transport and a forced connection error against a temp DB: returned False, CRITICAL line logged, row `('owner_alert', 'failed', 'boom')`. The function's docstring says callers treat the result as information by design. Residual (a design question, not a bug): nothing retries or escalates a failed alert; the only trace is the row and the log.

CLOSED 2026-10-04: the residual was fixed by #1104 (found already on main, not re-done here). `send_owner_alert` runs every caller through `deliver_with_retry`: retry on the desk's transient-fault policy, then one counted `owner_alert_undelivered` row with a running total. A deliberate mute is settled, never retried. The dashboard's muted-backlog read selects every `notifier_sends` row whose status is not 'sent', so an undelivered alert reaches the owner's only channel. Section kept: a caller that builds its own notifier and bypasses the funnel would escape this, and no guard forbids that yet.

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

### The evidence gate's per-name record has never been written -- FIXED

In the name-coverage loop the recording call passed a symbol argument twice --
once by name and once inside the unpacked details -- so every call raised a
type error, which a broad catch turned into a log line. A second collision sat
behind it: the stage was also passed twice, so removing only the symbol still
wrote nothing. Both were reproduced before the fix: the old code logged
"got multiple values for argument 'symbol'", and a run through the morning
session left zero per-name rows in the store. Fix: the details now carry
neither key. Proof: a new test runs the gate and asserts a real per-name row
for a candidate lands in the store with the right stage and outcome. The broad
catch stays, deliberately -- this runs on the trading path and a forensic
record may not stop it -- but it now logs the full traceback at error level,
and the store-level test is what keeps this class from hiding again.

VERDICT 2026-10-02 (isolation pass): ALREADY FIXED. Fixed by #1079 (fbae04e6) in `NameCoverageRecordSession`. Proof: restoring the pre-fix source makes `tests/test_name_coverage_rows_land.py` fail (1 failed); on main it passes (1 passed).

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

### `update_open_take_profit` refuses through an undefined name

DONE 2026-10-02. The refusal branches called a bare `_log` that the ledger
module never defined, so they raised `NameError`. They now use the module's
`logger`; `tests/test_take_profit_refusal_names.py` drives both refusals and
failed with `NameError: name '_log' is not defined` before the fix.

VERDICT 2026-10-02 (isolation pass): ALREADY FIXED by #1102 (c9c2dcbc), `src/storage/trades/ledger.py`. Proof: with the pre-fix ledger restored, `tests/test_take_profit_refusal_names.py` fails with `NameError: name '_log' is not defined` at ledger.py:1875; on main it passes and no bare `_log` remains in the ledger.
