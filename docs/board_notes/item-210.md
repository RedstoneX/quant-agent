## item 210 — the split plan was amended 2026-10-01 and now says when it runs

**Update, 2026-10-06.** The earlier count of 36 pending pull requests and the quoted file sizes are obsolete. At this audit there were none pending, and the two original oversized files had shrunk to 1,756 and 805 lines. That is substantial progress, not proof that every moved boundary and test still works. The next job is to check the actual split and its tests, name only the remaining gaps, and repair those gaps without beginning another wholesale rebuild. The desk remains off.

**Open.** The written plan for splitting the two oversized pipeline files was
re-measured against the current code on 2026-10-01 and amended: its figures
were all stale, one module boundary described a stop-moving method as if it
placed no orders (it has been reassigned to the protection module), two new
exit helpers were given a home, and a source-text check was re-pointed from
the wrong step to the right one. No code moved in that pass; it is a document
change only.

**When it runs.** Measured over the last fourteen days, 125 commits touched the
larger file — twenty inside the first step's range and sixty-three inside the
exits range — so a pure-move change survives about a day for the first step and
a few hours for the exits step. Ruling made on the risk route, 2026-10-01: the
split starts after today's trading sessions finish, not before, and the file
mapping is re-run immediately before the first step is opened. The owner
ratified the milestone and its ordering, not this timing call.

**Step 0 has landed (no code moved).** The first of the twelve steps adds only
guards, so it cannot collide with a trading session's own edits. Two things now
exist for every later step to lean on. The first is a frozen inventory of what
each of the two oversized files contains — every module-level function and
every method name on every class, read from the abstract syntax tree rather
than by importing, so a move that breaks an import still reports the truth.
Any change to that set fails the build with instructions: a deliberate move
re-records the inventory in the same change, and the diff of that record is
the reviewable statement of what moved where; anything else is a method that
vanished or was duplicated. A second check asserts no method name is defined
twice across the classes that will be combined into one object, because under
mixins the first base silently wins.

The second is a migration helper for the number ledger. Every ledgered number
is keyed by an id that embeds its module, so a move makes those ids lie, and a
new file left out of the ledger's scope list drops its numbers out of the guard
with no error at all. The helper plans a move, applies it, and — the part that
matters — re-reads the result afterwards and reports anything left behind,
duplicated or unscoped, so the rewrite is checked rather than trusted. Both
guards are proved able to fail: the inventory check is run against source with
a method added, renamed and removed, and the verifier against a deliberately
half-applied move.

**One correction to the plan, measured not assumed.** Section 5 says the ledger
carries 36 ids under the larger file and 22 under the smaller. Measured against
current main today: 34 and 22. The 36 is stale by two; the 22 is right. The
counts are now pinned by a test, so the next drift is a red build rather than a
discovery. Eleven steps remain and this item stays open.

## Step 1 — the prompt-facts mixin (2026-10-01)

`src/pipeline_prompt_facts.py` now holds `PromptFactsMixin`: 39 methods and three
module-level helpers lifted out of `src/pipeline.py` byte-for-byte, 3,432 lines of
method bodies in all. `src/pipeline.py` falls from 21,864 lines to 18,289. The class
gains one base and nothing else; every method keeps its name on `TradingPipeline`, so
the 338 construction sites and every `self._build_*` call are untouched.

The mapping was re-derived from the AST on the day, not read from the plan, whose
offsets were already dead: cluster I is today methods `_build_position_history`
(line 7415) through `_compute_recent_performance` (line 10636), plus the position-facts
trio from the review cluster.

**`_handle_ex_dividends` did not move.** It sits inside cluster I's line range and it
moves live stops, which is the one thing this module must not do; it is reassigned to
the protection module in step 2. Nothing else in the moved set places, cancels or
amends an order — the three hits for `stop_records` in the moved text are reads of the
desk's own stop records for display.

Re-exported from `src.pipeline` on purpose: `_PM_PROFILE_SYMBOL_CAP`,
`_missed_ops_quality_metrics` and `_valuation_signal_from`, because tests and prose
import them from there by that name. `_valuation_signal_from` is used only by moved
prompt builders, so it travels with them rather than with the exit-trigger helpers the
plan groups it beside.

The logger in the new module is bound to the name `src.pipeline` rather than
`__name__`, so every log record the moved code writes is unchanged.

Three test patch sites had to be re-pointed, and the plan predicted exactly this class
of breakage: `src.pipeline.et_today` no longer reaches a method that now resolves the
name in the new module. They failed loudly (day counts of 14 against an expected 1)
rather than silently, and are re-pointed at `src.pipeline_prompt_facts.et_today`.
`_get_sector` needed nothing: every moved use of it is already a function-local import.

Ledger: 25 ids moved from `src.pipeline.*` to `src.pipeline_prompt_facts.*` through the
step-0 helper, which verified the rewrite afterwards and reported nothing left behind;
the new file is in `SCOPED_PATHS`, and the pinned count for the old module drops from
34 to 9. One id names a nested function and needed a second pass to catch, which is
worth knowing before step 2. The method inventory was re-recorded in the same change
and the new module added to the tracked list.

Item 210 stays open: ten steps remain.

## Step 2 — the protection cluster (2026-10-01)

`src/pipeline_protection.py` (`ProtectionMixin`, 4,438 lines) carries clusters F and H
out of `src/pipeline.py`, which drops from 18,289 to 13,912 lines. Everything that
places, cancels, restores or reconciles a protective stop or a sell now reads in one
file: the 603-line stop-coverage reconciler, the owner alerts, `_repair_stop_coverage`,
`_submit_protected_sell`, the finalizers, the write-ahead cancel/restore legs, the
repeg and restore drains, `_reprotect_residual_after_partial_sell`, and the fill,
orphan-submit and stop-out reconcilers.

`_handle_ex_dividends` came here too, reassigned out of step 1's prompt-facts cluster
because it shifts live stops down by the dividend — a stop-moving method in a module
advertised as read-only is the boundary lying about touching money. Its two test files
were run against this step as the plan requires.

The move is pure: method bodies are the same text, the only edits are the enclosing
class line, the import block and the re-exports. The plan estimated ~3,600 lines; the
measured figure is 4,438, because the cluster has grown since the estimate was taken.

Re-exported from `src.pipeline` so existing imports keep working: `_WAL_SELL_SENTINEL`
(7 test files plus `src/pipeline_stages.py` and `src/execution/scale_in.py`),
`_market_is_open_now`, `_price_is_through_stop`, `_position_notional`,
`_classify_coverage_gap`, `_reconciled_exit_action`, and `_finite_float_or_none` — the
last of these is the one name the plan did not anticipate: it is a broker-fill float
coercion the plan left in `src/pipeline.py` with the other risk-number helpers, but the
moved code uses it, so it travels with the cluster and is re-exported back.

CORRECTION (2026-10-01). The reason first given for that travel — "a base module cannot
be imported by its own mixin" — describes the opposite direction and is wrong. A mixin
module importing its base module is exactly what Python forbids HERE, and only because
`src/pipeline.py` already imports `src/pipeline_protection.py` at module import time:
the back-import would close a cycle. So the helper could not stay behind and be imported
forward by the mixin; it had to move and be re-exported backward. Nothing else in
`src/pipeline.py` still reads a moved module-level name.

Eleven test patch sites were re-pointed at the new module: six `_market_is_open_now` in
the stop-coverage repair tests and one each in the unreadable-stop, exit-path-records
and fractional-sizing tests, two `et_now` in fractional sizing, and three `et_today` in
the ex-dividend tests. A source-text scanner that read `terminal_fail` out of
`src/pipeline.py` was re-pointed at the new file; three number-ledger prose citations
that quoted `src/pipeline.py` line ranges now past the end of the shortened file were
corrected to their current addresses.

Ledger ids: none. All nine ids naming `src.pipeline.*` belong to methods that stay, and
the nested-function case step 1 hit was checked for explicitly and does not arise here,
so the step-0 helper had nothing to migrate. `src/pipeline_protection.py` was still
added to `SCOPED_PATHS`, so its numbers stay under the guard, and to the tracked module
list of the inventory guard, which was re-recorded in the same change.

Held deliberately: this sits on step 1 and is not merged on a trading day.

Item 210 stays open: nine steps remain.

## Step 3 — the de-levering ladder (`src/pipeline_delever.py`)

The plan's cluster M, re-measured against the step-2 branch before anything was touched:
the twelve methods from `_live_delever_price` to `_alert_owner_delever_incomplete` moved
verbatim into `DeleverMixin`, 1,209 lines of the ~1,235 the plan predicted. `_sweep_symbol`
sits inside the cluster's range and stayed in the base class exactly as the plan directs;
the "Spec §11.2" banner comment moved with the ladder rather than staying above the cash
sweep it does not describe.

Two methods the plan listed in cluster M did NOT move, and this is the deviation to read:
`_install_sigterm_unwind` and `_restore_sigterm` raise `SessionTerminated`, which the plan
keeps in `src/pipeline.py`. Moving them would have forced the desk's session-control
exception into the de-lever module, or forced a lazy import into a body that must stay
byte-for-byte. They are 27 lines, they are called only from the morning body that stays,
and leaving them is the smaller lie about the boundary.

The risk-number coercion helpers `_optional_risk_number` and `_risk_number` travelled with
the cluster and are re-exported from `src.pipeline`, which still uses them in
`build_risk_config`. This is the same shape step 2 hit with its broker-fill coercion: a
small private helper the base and the mixin both read, and the mixin may not import the
base. Thirteen `src.risk.rules` names and `rank_verdicts` are no longer read by the base at
all and moved with the cluster; nothing patches them on `src.pipeline` today.

Three source-text scanners were re-pointed at the new file: the single-owner pin on
`apply_gross_ceiling(emit_trims=True)`, the single-assignment pin on
`gross_ceiling_deferred = False`, and their prose. No test patch site needed re-pointing:
the ladder, margin-policy and kill-switch suites patch methods on the instance, not module
names on `src.pipeline`.

Ledger: one id moved, `_force_delever`'s first inline factor, migrated with the step-0
helper and verified by its own verifier. The nested-function case step 1 hit was checked
for explicitly across all twelve moved names and does not arise. `src/pipeline_delever.py`
was added to `SCOPED_PATHS` and to the inventory guard's tracked modules, and the inventory
was re-recorded in the same change. Three number-ledger prose citations quoting
`src/pipeline.py` line ranges now past the end of the shortened file were corrected.

Held deliberately: this sits on step 2, which is itself unmerged, and nothing lands on a
trading day.

Item 210 stays open: eight steps remain.

## Step 4 — the held-position exit engine (`src/pipeline_exits.py`, `ExitEngineMixin`)

Clusters K and L of the plan, re-measured against the step-3 branch before anything was
touched: every offset the plan quotes was dead, so the spans were re-derived with an AST
pass. Twenty methods and one class-level memo attribute moved verbatim out of
`TradingPipeline`: the target-revision adjudication and its filing, structural protection
for a holding and the voicing of its break, exit-trigger substantiation, the
holding-discipline fact-check, the deterministic trails and their ratchet cooldown, the
event-risk block, the alignment exit (cache, scan, reading record, opened-today test and
per-holding verdict), the AI risk review of exits, the refusal and approval records, and
the midday executor. The module-level exit-trigger vocabulary moved with them:
`_HARD_TRIGGER_KEYWORDS` and `_reason_cites_hard_trigger`, plus `_actions_with_scan_fallback`
and `_reason_claims_alignment_exit`, whose only caller is the midday executor.

Pure move. Method bodies are byte-for-byte; the only edits are the enclosing class line,
the import block and the base-class list. Last night's alignment exit and range-trail work
travelled inside those byte-for-byte bodies and was not touched.

Deviation, reported rather than forced: `_atr_for_symbol` and `_constructor_cfg_or_none`
sit inside the cluster's range and did NOT move. `_atr_for_symbol` is the shared indicator
helper the plan warned about — it is read by the base class's `_evening_stop_proximity`
and by `PromptFactsMixin`, not only by the exits; `_constructor_cfg_or_none` is read by
`TradingPipeline.__init__`. The base keeps both and lends them back to the mixin, the same
base-lends-a-helper shape steps 2 and 3 each hit.

Guards: `src/pipeline_exits.py` added to the inventory guard's tracked modules and to
`SCOPED_PATHS`, and the inventory re-recorded in the same change. Four number-ledger ids
migrated with the step-0 helper and confirmed by its verifier; the nested-function case
step 1 hit was checked explicitly across every moved name and does not arise. The
measured `src.pipeline` ledger-id count in the step-0 guard dropped from eight to four.
Nine ledger prose citations quoting `src/pipeline.py` line ranges now past the end of the
shortened file were re-pointed by content match, not by arithmetic.

Re-pointed because the text they scan moved: `tests/test_shorts_emergency_close.py`'s
`getsource(TradingPipeline)` scan for the midday loop's `qty <= 0` guard, and
`tests/test_holding_discipline_intraday.py`'s module-source scan for
`holding_discipline_claim_check`, both now read the mixin. One monkeypatch of
`src.pipeline._reason_cites_hard_trigger` now patches `src.pipeline_exits`. The prose path
in `tests/test_prompts_contract.py` was corrected. Every moved name stays re-exported from
`src.pipeline`, so `tests/test_alignment_exit_wiring.py`'s imports are untouched.

Held deliberately: this sits on step 3, which is itself unmerged, and nothing lands on a
trading day.

Item 210 stays open: seven steps remain.

### 2026-10-01 — step 10 landed

`src/pipeline_stages.py` 10,709 → 4,977 lines. The four stage classes moved
verbatim into one file each: `src/stage_morning_research.py` (1,637),
`src/stage_decision.py` (710), `src/stage_risk.py` (1,360, `RiskStage` plus its
five private helpers and their two constants) and `src/stage_execution.py`
(2,081). PR 844 was already MERGED, so its caveat lapsed; the plan's line
offsets were dead and the ranges were re-derived from the AST.

`pipeline_stages` re-exports all eleven moved names through a module
`__getattr__`, so `from src.pipeline_stages import RiskStage` and
`patch("src.pipeline_stages.RiskStage")` still resolve, and it mirrors any
attribute set on it into the stage modules that hold the same name, so the 30
test patch sites on `src.pipeline_stages` (25 of them `compute_indicators`)
still patch the object the moved code calls.

Still open on item 210: steps 5–9 and 11–12 (`src/pipeline.py`, and the
remaining `pipeline_stages` helper files — rotation exec, entry orders, sizing,
earnings quality).

Landing records for steps 5 and 6 (moved here verbatim from the board item; note the older 'still open' paragraph above predates them):

STEP 5 LANDED 2026-10-01 (merged with main after step 7): `src/pipeline_risk_gate.py` (`RiskGateMixin`) carries cluster D plus `_refuse_queued_earnings_buys` -- 909 lines moved verbatim; `compute_indicators`, `_get_sector` and `HARD_BLOCK_RULES` now resolve against the new module, so the 5 patch sites in `tests/test_bugfixes.py` and the import in `tests/test_sector_cap_unresolved.py` were re-pointed in the same change; the one ledger id (`_has_actionable_signal_fn:factor[0]`) was migrated and the new module added to `SCOPED_PATHS` and to the method-inventory guard. STEP 6 LANDED the same day, also after step 7: `src/pipeline_admission.py` (`AdmissionMixin`) carries cluster C -- 11 methods, 510 lines moved verbatim; the 8 `src.pipeline._get_sector` patch sites that reach it were re-pointed and the 12 hard-risk-cap sites stay on `src/pipeline.py`.
