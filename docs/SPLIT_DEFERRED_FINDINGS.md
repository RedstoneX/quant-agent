# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

## The midnight clock bug (HALF DONE -- cost-circuit half still open)

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

STILL OPEN: the cost-circuit half. The original entry named thirteen failing tests but provides no test names. Reproduction attempts on 2026-10-02 failed by three methods: clock simulation with Python and SQLite moved together showed zero failures; CI runs covering 00:00-00:16 ET showed no cost-circuit test failures; and direct inspection identified no named failure. This entry must not be treated as a known defect until a specific failing test is named and reproduced.

VERDICT 2026-10-02 (isolation pass): holding-discipline half -- ALREADY FIXED (module-level stamp now read at run time; `tests/test_no_local_day_as_exchange_day.py` guard passes, no import-time `str(et_today())` stamp remains in that file). Cost-circuit half -- NOT REPRODUCED (carried forward from the earlier investigation; not redone). The note names no test, so it should not be treated as a known defect.

REPRODUCTION ATTEMPT 2026-10-04 (main 7d92965e): NOT REPRODUCED, a fourth method. An LD_PRELOAD shim moved the OS clock under BOTH Python and SQLite `'now'` (checked: Python and SQLite both read 00:06 ET), collected the five cost-circuit test files at 23:58 ET and ran them at 00:05 ET, single process. Result: 243 passed, 1 xfailed, 0 failed. The suite also covers the self-clear tests the clock module docstring names, which the single-clock indirection (#926) now pins together. No cost-circuit test fails across the crossing, so no cause was patched; do not reopen this without a named failing test.

## The evidence gate's per-name record has never been written -- FIXED

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

## Owner alerts are sent and the result thrown away

Nearly every caller discards the return value of the owner-alert send, so a
failed delivery is indistinguishable from a successful one. Fix: make the
callers honour the result, and prove a failed send is visible somewhere.

FIXED 2026-10-04 -- the 2026-10-02 verdict was WRONG, and recorded is not surfaced. A failed send does land in `notifier_sends` with status `failed`, but exactly one query reads that table for the owner's dashboard (`get_muted_backlog`) and it selected `status IN ('muted','filtered')`. Telegram is hard-muted, so a failed owner alert -- including the naked-position page -- reached NO surface the owner has, while its caller discarded the False. The enumeration had already drifted once (`filtered` was missing until #978); the read now selects by exclusion (`status <> 'sent'`), so non-delivery is defined once and a future status cannot fall through it. `failed_total`/`failed_count` are reported alongside the two deliberate drops and the dashboard line names them. Proof: `tests/test_muted_backlog.py::test_a_failed_owner_alert_reaches_the_backlog` fails with the old status list restored and passes with the fix. Measured exposure: in production `notifier_sends` from 2026-09-18 to 2026-10-01, 221 rows, statuses only `sent` (213) and `muted` (8); 39 owner alerts, all sent -- so this has not yet fired in the recorded window.

Residual, unchanged and still open: nothing RETRIES or escalates a failed alert. Not fixed here because with the global mute on, a retry cannot succeed through the same channel -- the surface is the channel, which is what this change repairs.

VERDICT 2026-10-02 (isolation pass): NOT A DEFECT as written -- the note's consequence is false. 22 of 34 `send_owner_alert` call sites do discard the return, but a failed delivery is NOT invisible: `send_owner_alert` logs CRITICAL before sending, and the transport records every attempt in `notifier_sends` with status `failed` and the reason. Reproduced with the real transport and a forced connection error against a temp DB: returned False, CRITICAL line logged, row `('owner_alert', 'failed', 'boom')`. The function's docstring says callers treat the result as information by design. Residual (a design question, not a bug): nothing retries or escalates a failed alert; the only trace is the row and the log.

## A broker read error reads as "no stop to adjust" -- FIXED

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

VERDICT 2026-10-02 (isolation pass): INCORRECT. Earlier verdict read only the `class LLMCostCircuitBreaker(...)` line; the parts ARE already standalone. `src/cost_circuit/parts/` has eleven standalone classes (merged #1064, #1066, #1070 on 2026-10-02); `tests/test_cost_circuit_parts_boundary.py` passes 35 tests, building all eleven from stubs with no breaker composition. The `breaker_*.py` files are thin per-call shims. ONE genuine gap remains: the breaker still INHERITS the shims rather than HOLDING part instances; that conversion changes every test patch target, so it is a separate deliberate instalment.

## Real boundaries still owed

The position builder, the portfolio-manager seat and the prompt-facts review
chunk are under the ceiling but are not separable pieces. Same treatment.

UPDATE 2026-10-04: `PromptFactsReviewMixin` is now shims only; its eleven bodies
live as five constructed parts under `src/prompt_facts/review/`, each witnessed in
`tests/test_prompt_facts_parts_boundary.py`. `PromptEvidenceMixin` bodies already
live on `PromptEvidence` (witnessed); the agent still INHERITS the shim class, and
61 test sites call those names on the agent class, so dropping the inheritance is a
separate change.

DONE 2026-10-04 for `PromptEvidenceMixin`: `PortfolioManagerAgent` no longer inherits it. `hold_prompt_evidence` builds one `PromptEvidence` and installs classmethod delegates on the agent (61 class-level test call sites counted, all still resolving, none rewritten); `tests/test_portfolio_manager_parts_boundary.py` builds and runs the part on a bare class. Three mixins remain on the seat (`DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`) plus `PromptFactsReviewMixin`.

DONE 2026-10-04 (later): the three remaining PM-seat mixins are gone. `PortfolioManagerAgent` inherits only `LiveLimitPrompt` and `BaseAgent`; it holds one `CandidateRanking`, `RotationSection` and `DecisionGrounding` built at import by `hold_*` functions in the former mixin modules, with every host-read collaborator handed in live (see docs/ARCHITECTURE.md, PM-seat third instalment). No function body moved (the bodies already lived on the parts); the two class attributes that moved match the trunk AST (2 of 2). Nothing under `src/agents/portfolio_manager/` is a mixin any more; of the pieces this entry named, only `PromptFactsReviewMixin` (shims only, on the pipeline, not the seat) still inherits.

DONE 2026-10-04 for the position builder: `PortfolioConstructor` inherits from nothing (`__bases__ == (object,)`). `_StopMixin` is now `StopRules` (`src/portfolio_constructor/stops.py`, keyword-only `read_cfg` getter + `entry_stop_resolver` factory, both read live) and `_OrderBuildMixin` is now `OrderBuilders` (`src/portfolio_constructor/orders.py`, one keyword-only `collaborators` callable read per call); `src/portfolio_constructor/assembly.py` holds both on the owner at construction and installs same-named thin shims on the class, so every existing call site and every instance patch still resolves. Witness: `tests/test_portfolio_constructor_parts_boundary.py` builds and exercises both parts from stubs with no `PortfolioConstructor` existing, and `check_boundary` passes for `stops`, `orders` and `assembly`. Full suite after the change: 10,150 passed, 0 failed (10,149 + the one prompt-binding anchor repointed from `_StopMixin` to `StopRules`). Honest limit: the bodies themselves did not move -- `StopRules` still carries the seven stop-rule bodies and `EntryStopResolver` (1,080 lines) remains the real mass; what changed is that the owner holds parts instead of inheriting bags. The 1,676-line `src/portfolio_constructor/__init__.py` is still the owner object and is NOT a boundary; its own split is a separate instalment.

VERDICT 2026-10-02 (isolation pass): REPRODUCED (structural debt). Mixins remain: `PromptFactsReviewMixin` (`src/pipeline_prompt_facts_review.py`) and in `src/agents/portfolio_manager/`: `DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`, `PromptEvidenceMixin`; `tests/test_boundary_harness.py` passes (with the other two files run, 20 passed) but only covers pipeline mixins. The note named three pieces; the portfolio-manager seat actually has four mixins. Rebuild size: medium-large, five pieces.

DONE 2026-10-04 for the technical seat's re-read cache: the cost circuit itself was re-checked and is already eleven HELD parts (section above), so the next mixin with a real body was taken. Measured reach-backs into `self` across every remaining mixin that still carries bodies: `TechRereadMixin` 4 names / 6 sites (`_analyze_batch_uncached`, `last_carried`, `last_unanswered`, `last_unreadable`); next-best `ShiftStopsMixin` 6 names / 8 sites; everything else with bodies is over 1,000 lines (`ExitEngineMixin` 17 names, `DeleverMixin` 11, `IntradayMixin` 43, `PromptFactsMixin` 11, `ProtectionMixin` 10). `TechRereadMixin` is now `TechReread` (`src/agents/tech_reread.py`): `ask` is a getter read per call (an instance-bound spy must be what it calls); `state` is the owner held directly (its identity never changes, and the pipeline reads `last_carried` off the agent). `TechAnalystAgent.__bases__ == (BaseAgent,)`; `hold_tech_reread` installs the one thin `analyze_batch` shim, built per call because two tests make the agent with `__new__`. Body matches trunk by AST after normalising the two seams. Witness: `tests/test_tech_reread_parts_boundary.py`, which never imports the agent. The size ratchet refused the shim's growth, so `_record_answer_hygiene` and its two constants moved verbatim to `src/agents/tech_answer_hygiene.py` (3 of 3 AST-identical). Still mixins with bodies: `ShiftStopsMixin`, `ExitEngineMixin`, `DeleverMixin`, `IntradayMixin`, `PromptFactsMixin`, `ProtectionMixin`; shim-only (bodies already in parts): `PromptFactsReviewMixin`, `ResearchContinuityMixin`, `RiskGateMixin`, `AdmissionMixin`, `DecisionGroundingMixin`, `RotationSectionMixin`, `CandidateRankingMixin`.

## `update_open_take_profit` refuses through an undefined name

DONE 2026-10-02. The refusal branches called a bare `_log` that the ledger
module never defined, so they raised `NameError`. They now use the module's
`logger`; `tests/test_take_profit_refusal_names.py` drives both refusals and
failed with `NameError: name '_log' is not defined` before the fix.

RESOLVED 2026-10-04 (class closed, entry retired). The instance was already fixed; the CLASS was not. The mechanism is a verbatim lift that leaves a module-level name behind: the bodies move, the imports do not, and the only caller sits inside a broad `except Exception`, so the `NameError` is logged as a write failure and nothing ever fails. A repo-wide check now exists -- `tests/test_undefined_name_guard.py` parses every module under `src/` and fails on a load of a name no enclosing scope, module binding or builtin provides, shrink-only baseline. It found one more live instance of the same class: `src/exits/exit_substantiation.py` called `seat_acceptance_kwargs` and `agent_log_kwargs` without importing them, so the position-reviewer re-ask agent-log row has never been written since that lift; the import is restored. One documented lazy-wiring case (`PortfolioManagerAgent` in `src/agents/portfolio_manager/ranking.py`, already `# noqa: F821`) is baselined, not exempted.

VERDICT 2026-10-02 (isolation pass): ALREADY FIXED by #1102 (c9c2dcbc), `src/storage/trades/ledger.py`. Proof: with the pre-fix ledger restored, `tests/test_take_profit_refusal_names.py` fails with `NameError: name '_log' is not defined` at ledger.py:1875; on main it passes and no bare `_log` remains in the ledger.

DONE 2026-10-04 for the ex-dividend stop shift: `ShiftStopsMixin` is gone. Its one body is `StopShifter.shift_stops_down` in `src/execution/broker_parts/stop_shifter.py` (AST-identical to trunk, proven by script), taking seven keyword-only collaborators (`client` for the market-closed gate plus the six cluster methods). `stop_shift.py` is now a shim of two functions: `build_stop_shifter(placer)` reads each collaborator off the placer PER CALL, and `shift_stops_down(placer, symbol, amount)` is bound onto `StopPlacer` as a class attribute, so `StopPlacer.__bases__ == (object,)` and the broker's `shift_stops_down` shim is untouched. One marked lazy mirror in the shim keeps `_quantize_price`/`defer_shift_if_closed`/`logger` resolving. Witness tests in `tests/test_broker_parts_boundary.py`: the part runs the amend path end to end on stubs with no placer and no broker; a collaborator swapped on the placer after construction is the one the body calls (caching the part made that test fail; restoring made it pass). Still mixins with bodies: `ExitEngineMixin`, `DeleverMixin`, `IntradayMixin`, `PromptFactsMixin`, `ProtectionMixin`.
