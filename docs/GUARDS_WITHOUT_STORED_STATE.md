# Guards that store nothing — the plan of record

Owner mandate, 2026-10-02: "Yes, rip it all out. Rip all out the stored
bookkeeping. And rebuild it properly so it's not a ticking time bomb." This is
the specification. Nothing else is started until it is built and proven.

## Status, re-measured 2026-10-04 on `origin/main`

The mandate is built for every guard this document names; sections below are
kept as history and marked DONE where they describe a stored file that is gone.

- Stored baselines: none. No baseline, snapshot or known-offender file exists
  under `scripts/` or `tests/`; the only JSON/TXT there are test fixtures.
- SUPERSEDED 2026-10-08 (owner ruling: no limit re-measured against `origin/main`): seven structure
  checks now use FIXED lists, `config/check_allowlists/struct_<check>.txt`, one identity per line,
  shrink-only; a new entry needs a `Guard-rule-change:` commit line and a stale entry fails. They are
  `pipeline_method_guard`, `pipeline_new_guard`, `replay_outbound_guard`, the module-accumulator test,
  the patch-target test, the boundary harness test and the desk-output audit. Mentions of those
  guards reading `origin/main` below are history.
- Fixed limits, not trunk-derived (2026-10-08): file size and line width are no
  longer a trunk ratchet. ruff enforces line width 120 and per-function complexity
  10, statements 50 and branches 12, all written in `pyproject.toml` and run by
  `tests/test_ruff_clean.py`. Files that already broke a limit are listed there by
  path and rule; the list may only shrink. The old ratchet's limits moved whenever
  trunk moved, reddening unrelated changes and rewarding line-cramming.
- Computed at check time against the trunk: 14 comparison guards
  (`import_graph`, `pipeline_new_guard`, `pipeline_method_guard`,
  `silent_swallow_guard`, `local_day_guard`, `replay_outbound_guard`,
  `unscoped_number_guard`, `board_rot_guard`, `board_item_guard`,
  `settlement_fill_guard`, `statement_cram_guard`, `guard_weakening_gate`, the
  boundary harness, the patch-target audit) each reference `guard_reference` or
  `origin/main` [measured: grep for `guard_reference|trunk_rev|origin/main` in each file].
- Absolute-rule checks with no trunk comparison and no stored list: `disk_guard`,
  `test_undefined_names_guard`, `test_holding_discipline_guard`,
  `test_stop_read_unknown`, `test_money_path_guards_are_loud`.
- RESOLVED KEPT RECORD (2026-10-04): `tests/test_one_definition_guard.py`'s
  registry and its `KNOWN_GOOD` set are reviewed policy -- each entry carries a
  reason, a test fails when a named site stops existing, and no scan could
  produce "this duplicate is legitimate". Not a cached measurement; stays.
- Python-clothed baselines found by a second sweep 2026-10-04 (shapes searched:
  pinned integers compared against, per-path count tables, hardcoded paths that
  tell a guard where something lives; method: grep over `scripts/` and `tests/`
  for `== N`, `MAX_*`, `*_BASELINE`, `*_ON_ARRIVAL`, dict/frozenset literals in
  guard files, and `"docs/|config/|tests/|src/"` path strings in `scripts/*guard*.py`):
  - ~~48 per-file finding ceilings in `tests/desk_output_guard.py`~~ DONE -- the
    counts (16 in `ALLOWED`, 32 in the benchmark-results table) are deleted; the
    reasons stay as policy. The audit scans each allow-listed file as it stands
    on `origin/main` via `scripts/guard_reference.trunk_blobs` and fails on any
    finding identity (signal + offending line) the working copy holds more of,
    so a swap of one real order id for another now fails where a count passed.
    One ceiling had drifted loose (12 recorded against 9 present: three real
    values could have been added unseen). Refuses when the trunk is unreadable.
  - ~~`SELF` in `scripts/local_day_guard.py`~~ DONE -- a hardcoded path excusing
    the guard's own test file, which produces zero offences when scanned
    (measured 2026-10-04), so the exemption excused nothing and is deleted.
  - ~~`CEILING = 2561` and `WIDTH = 120` in `scripts/file_size_guard.py`~~ DONE
    -- both integers are deleted. They were statistical fences measured once
    (2026-10-01 and 2026-10-04) and written down: stored bookkeeping and
    made-up numbers at the same time. Both are now DERIVED on every run from
    the populations themselves -- the line ceiling as Q3 + 3*IQR of tracked
    `.py` file lengths, the width fence as the 99.9th percentile line width --
    computed TWICE, once over the working tree and once over `origin/main`,
    with the TIGHTER of the two used. That clamp closes the gaming case the
    earlier sizing worried about: deleting many small files raises Q3 and
    padding lines just under the fence raises the percentile, but the trunk's
    own value is computed in the same run and still binds, so a branch may
    only tighten a fence, never loosen one. Re-measured on `origin/main`
    2026-10-05 [measured: 1,133 tracked `.py` files, Q1 91, Q3 401, IQR 310,
    Q3+3*IQR = 1,331; 418,552 lines, p99 90, p99.9 120, 411 lines wider]: the
    width fence is unchanged at 120 and the line ceiling TIGHTENS from 2,561 to
    1,331. The ceiling only bites a file CROSSING it (`size > ceiling >= was`),
    so the 56 files already above it are governed by the growth rule as before.
    The ceiling is clamped never to fall below `FLOOR` (400, the owner's
    ceiling for a NEW module), and both derivations REFUSE on an empty
    population rather than invent a number. Tests:
    `test_neither_fence_is_a_stored_number`,
    `test_padding_lines_cannot_drag_the_width_fence_out`,
    `test_deleting_small_files_cannot_raise_the_line_ceiling`,
    `test_the_ceiling_never_falls_below_the_new_module_floor`,
    `test_it_refuses_rather_than_invent_a_fence_from_nothing`.
  - DONE: the money-module list in `scripts/silent_swallow_guard.py` is no longer a hand-kept
    23-path list. `scripts/money_modules.py` derives it at check time from the installed SDK's
    exchange-writing methods and the call graph under `src/`; the derived surface is far larger
    (it caught modules the old list never named) and it refuses if the SDK cannot be read.
  - DONE: the board is found by shape (`scripts/board_locator.py`) in `board_item_guard` and
    `board_rot_guard`; the ledger is found by shape (`scripts/ledger_locator.py`: the one YAML
    with a top-level `numbers:` whose rows carry `site:`) in `ledger_prose_guard`,
    `ledger_substantiation_guard` and `settlement_fill_guard`. Zero or several matches REFUSE.
  - NOT converted, still named by path (open): `src/number_sources.load_ledger` (runtime config loader, not
    a guard), and the board path in `definition_of_done`, `check_board_hygiene`, `board_numbers`
    and `next_board_number`.
- Config files checked against the three-way test (kept record of a decision /
  stored baseline-allow-list a guard could compute / neither). Read 2026-10-04:
  - `config/number_ledger.yaml`: KEPT RECORD. The register of money-governing
    numbers and their provenance; real content, stays.
  - `config/live_capital_preflight_attestations.yaml`: KEPT RECORD. A named
    person's attestation of conditions a machine cannot verify; cannot be computed.
  - `config/number_ledger_history.yaml`: OVERTURNED 2026-10-05, DELETED. The
    2026-10-04 ruling kept it on the grounds that failing a FALL as well as a
    rise made it stricter than a trunk comparison. That reading missed the
    direction that matters: summing deltas means the total accepts a POSITIVE
    one, so the reference could be raised by the very change it was refusing,
    and on 2026-10-05 open change 1430 did exactly that — it hit 131 against
    127 and appended `+4`. Failing a fall is not strictness either; the
    standing order is to drive the arbitrary count to ZERO, so a fall is the
    goal. The arbitrary-number ratchet now counts `status: arbitrary` rows in
    the trunk's own `config/number_ledger.yaml` at check time and refuses only
    a RISE. Deletion-gaming stays covered by `scripts/unscoped_number_guard.py`.
  - `config/number_ledger_route_history.yaml`: DELETED. The settlement-route
    ratchet is keyed on row identity against the trunk's ledger at check time:
    a routeless `arbitrary` row must already be routeless on the trunk, and
    an unreadable trunk refuses.
  - `config/prompt_only_numbers.yaml`: RULED KEEP (2026-10-04). Test applied:
    the figures present in a sheet ARE derivable (the test's shape list finds
    them), but each row's status and open question is a human judgement that a
    number is prompt-only and unsettled, which no scan can produce. Staleness is
    covered both ways by the tests: a new figure with no row fails, and a row
    whose figure left the sheet fails. REAL GAP, not a tidy close: nothing
    detects a number that stops being prompt-only while its prose stays (for
    example it becomes code-computed or ledgered), and a `sourced` status is
    never checked against anything. The shape list is also a known-string
    scan, so a reworded figure is missed (the test file says so).
- Source-reading tests, re-counted 2026-10-04: 119 of 480 test files by a broad
  text heuristic (AST/getsource use, or file reads combined with a source-path or
  git-listing pattern), 44 by a strict one (AST/getsource AND a repo path)
  [measured: grep over `git archive origin/main tests`, top-level `test_*.py`].
  The count cannot cleanly separate reading source from running code: the broad
  figure errs HIGH (fixture reads, patch-target strings containing `src/`), and
  both miss tests that call a guard script which does the scanning. The 154-of-384
  figure below used a different method on a smaller tree and is not comparable.
- Acceptance 1-4: the size ratchet's own tests exist; the other guards were not
  each re-proven against all four criteria in this pass, so that is unverified
  here rather than assumed.

## Why, measured (2026-10-02, original table, kept as history)

A read-only sweep of all 19 then-open changes, 2026-10-02:

| Finding | Measured |
|---|---|
| Red changes failing on shared bookkeeping, not their own code | 8 of 11 (73%) |
| Tracked files sitting at EXACTLY their recorded size cap | 291 of 292 |
| That day's commits touching the one shared 293-entry baseline | 48% |
| Test files reading source TEXT/AST/paths rather than running code | 154 of 384 (40%) |

(Historical, 2026-10-02; the baseline file and the test no longer exist.) `test_baseline_is_tight` made leftover headroom a FAILURE, so headroom was
illegal by construction. Adding one line anywhere in the tree therefore forced
an edit to a single shared file that every other open change is also editing.
That was not bad luck; it was arithmetic, and it is why changes jammed all day.

Branch staleness was investigated and is NOT causal: merging main into a red
change did not make it green.

## The property that fixes it

**A guard stores nothing.** It cannot then collide with another change, go
stale, rot silently, or be quietly edited until it passes.

Each guard today asks "is this worse than the recorded state?" and keeps the
record in the repo. It should ask **"is this worse than what is on main right
now?"** and compute the answer twice at check time — once against the working
tree, once against `origin/main` — comparing the two. No file, no record, no
shared write.

`.github/workflows/test.yml` already checks out with `fetch-depth: 0`, so the
reference is available in CI today. A local run fetches `origin/main` or
refuses.

## The five rules

1. **Store nothing.** No baseline file, no pinned set, no known-offenders list
   committed to the repo.
2. **Compare against `origin/main`, computed fresh at check time.**
3. **If it cannot compare, it REFUSES.** It never passes by default. A guard
   that silently passes when its reference is missing is decoration, and this
   is the single most likely way to get this rebuild wrong.
4. **Report the delta, not the absolute.** The message says what this change
   made worse, which is the only thing the author can act on.
5. **Compare identities, never totals.** A delta expressed as a count has a
   hole: a change that removes one offender and adds a different one nets to
   zero and passes, so the new defect lands unnoticed (found 2026-10-02 when
   proving one guard red needed two added offenders because the branch had
   removed one). Every scanning guard names each site — path, kind, enclosing
   scope, the site's own source text — and fails any identity the working tree
   holds more copies of than `origin/main` (`guard_reference.added_sites`).
   Removals are never a failure. The file-size guard is numeric by nature; its
   identity is the path, so a shrink in one file never offsets growth in
   another.

## What comes out

| Stored file | Read by |
|---|---|
| ~~`tests/file_size_baseline.json`~~ | DONE — deleted with `scripts/regen_file_size_baseline.py` and `test_regen_baseline_cannot_drop_a_trunk_file`; replaced by `scripts/file_size_guard.py` + `scripts/guard_reference.py`, which measure the working tree and `origin/main` at check time |
| ~~`tests/import_cycle_baseline.json`~~ | DONE — deleted with `--shrink-baseline`/`--seed-baseline` and `test_baseline_only_shrinks`; `scripts/import_graph.py --check` now builds the graph from the working tree and again from `origin/main` via `scripts/guard_reference.py` and fails on any cycle edge (importer, imported) that is new — an edge identity, never a count |
| ~~`tests/import_layers.json`~~ | DONE — deleted; the `broker-seam` rule now lives in code (`LAYER_RULES` in `scripts/import_graph.py`) and the guard fails any importer of `src.execution` that `origin/main` does not already have, by (importer, imported) identity |
| ~~`tests/pipeline_new_baseline.json`~~ | DONE — deleted; `scripts/pipeline_new_guard.py` now names each `TradingPipeline.__new__` site in the working tree and on `origin/main` at check time and fails on any new site identity |
| ~~`tests/silent_swallow_baseline.json`~~ | DONE — deleted; `scripts/silent_swallow_guard.py` now names each silent-swallow site in the money modules in the working tree and on `origin/main` via `scripts/guard_reference.py` and fails on any new site identity (never on a total, so a swap of one offender for another still fails); a handler's durable record is recognised by the IDENTITY the call binds to (`scripts/swallow_resolver.py`: import aliases followed, foreign modules never count, a local def judged by its body, star-imported or undefined names unknown), never by the call's spelling |

`tests/test_baseline_merge_driver.py`, `scripts/resolve_baseline_conflict.py`
and `scripts/git_merge_driver_baselines.sh` existed ONLY to manage collisions
between stored baselines; all five files are gone, so all three are DELETED. A merge driver for a
file that no longer exists is the clearest possible sign the file should not
have existed.

Same class, same treatment, after the five land:
- ~~the known-leaks list in `tests/test_no_silent_patch_targets.py`~~ DONE — no list; the patch-target audit runs over the working tree and over `origin/main` at check time and fails on any unreachable-call-site identity that is new
- ~~`_KNOWN_CHECKBOX_FINISHED_ITEMS_2026_09_26` in `tests/test_status_board.py`~~ DONE — the frozenset is deleted and `tests/test_status_board.py` asserts it stays gone; `scripts/board_rot_guard.py` reads the finished-but-still-open items in the working tree and on `origin/main` via `scripts/guard_reference.py` at check time and fails on any item identity that is newly flagged, refusing when `origin/main` cannot be read

Measured 2026-10-04 on `origin/main`: every entry above is struck through. No check in `scripts/` or `tests/` reads or writes a committed baseline, a pinned offender count, a saved snapshot or a known-bad list; `scripts/guard_reference.py` holds no cache, and the guard test files pass under the project's parallel `-n auto` run. The list is complete; a new entry here means a new guard was written the old way.
- ~~the offender baseline in `tests/test_no_local_day_as_exchange_day.py`~~ DONE — the hardcoded `_BASELINE` is deleted; `scripts/local_day_guard.py` scans the working tree and `origin/main` at check time and fails on any new site identity
- ~~`tests/replay_outbound_sites_baseline.json`~~ DONE — judged a cached
  measurement (an AST scan of `src/` frozen on 2026-10-02, no human
  reasoning in any entry), so it is never created; `scripts/replay_outbound_guard.py`
  scans `src/` in the working tree and on `origin/main` at check time and
  reports the delta. The policy half — the list of module names that mean
  "this can leave the box" — stays in code, where it is reviewed.
- ~~the unscoped-number ceiling in the number-sources guard~~ DONE — `MAX_UNSCOPED_NUMERIC_SITES` and its pinning test are deleted; `scripts/unscoped_number_guard.py` runs the unscoped scan on the working tree and on `origin/main` at check time and reports the delta per NUMBER (a site new to the tree is excused only by a lost trunk site with the same value and the same name or module — a move or rename; a delete never pays for an unrelated add)

Not in this class: `config/number_ledger.yaml`. That is real content — the
desk's justification for numbers that govern money — not a cached measurement.
It stays.

## The shared helper

One module both the tests and the scripts use. Given a measurement function
that maps the tree to `dict[str, int]` (or a set), it returns the measurement
for the working tree and for `origin/main`, and the guard compares them. Each
existing guard keeps its own measurement logic unchanged; only the source of
"what it was before" changes.

Getting main's version of the tree: `git ls-tree`/`git show` against
`origin/main` for a file-level measurement, or a throwaway worktree for a
whole-tree one. Measure which is faster before choosing — a guard slow enough
to be skipped is a guard that gets skipped.

## One moment on both sides of the comparison

The reference is **not** always `origin/main`'s current tip. On `pull_request`
CI the tree under test is GitHub's merge ref — the branch merged into whatever
main was when GitHub last computed it — so comparing it against a freshly
fetched `origin/main` measures a stale tree against a newer trunk and bills
main's own later commits to the branch. Measured 2026-10-04 on PRs 1158 and
1172: the merge ref's main-side parent was ten commits behind main, the tested
tree held `src/pipeline.py` at 2529 lines where main held 1894, and the
file-size ratchet reported a 600-line growth on a file neither branch touched.
The same phantom appeared on 17 of 18 red changes.

`guard_reference.trunk_rev()` therefore resolves the main-side parent of the
merge commit actually under test, and every guard reads the trunk through it.
A branch is judged against the real main it was merged with.

This is deliberately **not** "compare against the merge base". A merge base is
a commit the branch picks by never merging, which would let a branch delete
something today's main still needs and pass. The merge ref's first parent is
recomputed by GitHub from current main and the branch cannot influence it, and
it is trusted only when all four hold: the run is a `pull_request` event, HEAD
is a two-parent merge, HEAD's second parent is exactly the PR head commit the
event payload names, and HEAD's first parent is an ancestor of the current
`origin/main`. Anything else — a direct push, a local run, a missing or
mismatched payload — falls back to `origin/main`'s tip, which is the stricter
reference, so no branch gains anything by making the detection fail. Nothing is
stored either way — not even in memory: the reference is resolved afresh on
every call, because a cached tip read earlier in the process let the refusal
test pass with an unreadable trunk (CI, 2026-10-04). The refinement only ever
applies to a trunk that WAS read; it never stands in for one that could not be.

**Only the branch's own files are judged (2026-10-05).** The merge ref fixed a
stale TREE; a second phantom remained with a fresh one. While a split lands,
the trunk shrinks minute by minute, and a branch that never opened the split
file still carries the trunk's older, larger copy; measured against today's
trunk it reads as growth the branch never wrote. Measured on two branches the
same night: the pipeline, the rotation executor and the backtest engine went
red within the minutes between an agent merging trunk and running the tests,
and one branch carried a phantom and a real growth in the same run. The size
ratchet now skips any file whose working text is byte-identical to the merge
base's copy (`guard_reference.untouched_paths`): a merge takes the trunk's side
of an unchanged file, so the branch contributes nothing to it, and measuring it
is measuring trunk against trunk. This is the merge base's one legitimate use —
deciding whether the branch wrote to a file at all, never what to compare
against. Any difference, one deleted line included, makes the file the branch's
own and it is judged in full against the CURRENT trunk exactly as before, so
touching a file to own it buys nothing and the real component of a mixed run
still fails. No totals cross files; with no merge base nothing is excluded.
Tests: `test_trunk_shrinking_a_file_the_branch_never_opened_is_not_growth`,
`test_touching_a_file_puts_it_back_under_the_full_rule`. The width ratchet
lives in the same function and is covered by the same skip (the live case
showed two "new" wide lines the trunk had removed). The statement-cram ratchet
shares the shape — a stale copy of a file the trunk has since uncrammed — and
applies the same skip in `statement_cram_guard.violations`; tests
`test_trunk_uncramming_a_file_the_branch_never_opened_is_not_a_violation`,
`test_touching_a_file_puts_it_back_under_the_full_cram_rule`.

## Acceptance — proven, not asserted

1. **Two unrelated changes at the same time never collide.** Branch twice off
   main, add a line to a different file in each, and merge both. This must
   succeed with no conflict. (2026-10-02: it could not, because both edited the baseline.)
   This is the test the owner was promised.
2. **Each converted guard still catches what it caught.** Before deleting a
   baseline, record what its guard currently flags; after conversion, the same
   change must still be flagged. A guard that goes quiet has been deleted, not
   converted.
3. **Every converted guard REFUSES when `origin/main` is unreachable.** Test it
   by pointing the remote at nothing.
4. **No tracked file sits at exactly its cap afterwards, because no cap is
   recorded.** The 291-of-292 number should become meaningless.

## Order (DONE for the size ratchet; the rest followed, see Status)

Convert ONE guard end to end first — the size ratchet, which causes most of the
pain — and prove all four acceptance criteria on it before touching the others.
A half-converted set is worse than either the old one or the new one.

Also done: ~~`tests/pipeline_method_inventory.json`~~ (a script-regenerated measurement, not policy) is deleted with its `--write` mode; `scripts/pipeline_method_guard.py` names each owner of a method defined on 2+ of `TradingPipeline` and its mixins, in the working tree and on `origin/main`, and fails any new identity. A move that leaves one copy never fails.

Also done (2026-10-02, Python-clothed baselines): ~~`TRADING_PIPELINE_TEST_FILE_BASELINE = 79`~~ in `tests/test_boundary_harness.py` (a count measured on 2026-10-01, a TOTAL) is deleted; `tests/boundary_harness.py::trunk_test_files_referencing_pipeline` now names each test file that names `TradingPipeline` in the working tree and on `origin/main` at check time and fails on any new path. The skip of the harness's own files, the composition root and the one deliberate whole-system test is unchanged and is policy, not a measurement. ~~`_WORK_MD_OVERSIZE_ON_ARRIVAL`~~ / ~~`_WORK_MD_POINTERLESS_ON_ARRIVAL`~~ in `tests/test_status_board.py` (eight item sizes and four item numbers measured on 2026-10-01) are deleted; `scripts/board_item_guard.py` names each (item, rule) offence -- over the per-item budget, or without a resolving note pointer -- on the working tree's board and on `origin/main`'s, and fails on any new identity. The budget's divisor (40 open items) stays: it is a chosen policy figure, not a measurement. One consequence stated plainly: an item that is already over budget on the trunk may be edited, including grown, without failing -- pre-existing rot is the trunk's, not the change's, exactly as `board_rot_guard` treats finished-but-open items. Both refuse without `origin/main`; removals never fail.

## Weakening a guard needs a written reason

`tests/test_guard_weakening_gate.py` (logic in `scripts/guard_weakening_gate.py`) fails any change that edits or deletes an existing guard file without a one-line `Guard-rule-change:` of 25+ words in a commit message. Guard files are derived by naming rule (`scripts/*guard*.py`, `tests/test_*guard*.py`, `tests/test_*ratchet*.py`), never listed. Tightening cannot be told from loosening, so every behavioural edit is asked; only new guard files and docstring/comment/format-only edits (identical AST) are exempt. An unreadable base is a failure.

Also done (2026-10-04, the settlement-recording class): `scripts/settlement_fill_guard.py` refuses a NEW site that hands a `built` settlement route's field to the writer through a three-argument `getattr` or a literal `None`. It stores no list of known offenders -- it names each offending (file, enclosing scope, field, shape) in the working tree, names them again on `origin/main`, and fails only on an identity the tree holds that the trunk does not; it refuses outright when the trunk cannot be read. This is the layer ABOVE the one `tests/test_settlement_recording_writes.py` checks: that test reads the storage layer's AST and asks whether anything writes the column, and it answered YES, correctly, for all four recordings that nonetheless recorded nothing. The defect was always the caller's expression, and a defaulted `getattr` is the one expression that cannot fail. What this guard deliberately does NOT decide is whether a write is CENSORED by the branch it sits on (the noise band, PR #1177, wrote only where the band blocked an exit); that is a question about the meaning of a condition, not its shape, and what settles it is a production measurement that the accrued sample holds observations on both sides of the threshold.

Also done (2026-10-05, settlement route field identity check): The ledger route validation in `src/number_sources.py` was changed from matching column names as words inside three hardcoded storage-file path strings, to keying on identity via `src/storage_write_index.py`. The index parses every module under `src/` and resolves the table and column list of each INSERT and UPDATE statement it can read statically; the route's `<table>.<column>` must match one of those identities. Nothing is stored; writer files are not listed; the old rule confused "this token appears somewhere in a file path" with "this table's column is written", admitting columns no statement writes and refusing genuine writes living outside the three listed files. It refuses when `src/` is unreadable.

## Compressing is the same offence as growing

`tests/test_statement_cram_ratchet.py` (logic in `scripts/statement_cram_guard.py`) closes the route a change took on 2026-10-04 to satisfy the size ratchet without splitting anything: it joined statements onto shared lines (`from A import x; from B import y`, `if cond: return x`) and only the line counter moved. The guard PARSES every tracked `.py` file (the size ratchet's own scope, `working_paths("*.py")`, no second list) and names each line on which more than one statement starts, or whose block body sits on its header's line (`if`/`elif`/`except`/`else`/`finally`/`case` headers alike); semicolons inside strings, docstrings and comments are invisible to it. There is no threshold -- the measure is statements per line -- and no stored list: identities (`path`, enclosing scope, the line's text) are collected on the working tree and on `origin/main` at check time and only a NEW or more-frequent identity fails. A one-line stub body (`class Boom(Exception): pass`, `def f(self) -> int: ...`) is not cramming and is exempt. It refuses without `origin/main`; removals never fail; the ~116 pre-existing crammed lines on the trunk pass (measured 2026-10-04).

**Lines are not size (2026-10-04, second route).** The same day, two changes added error logging to dozens of money-path sites, reported their files SHRANK in lines, and between them added 33 lines over 140 characters with none removed (measured from the two diffs); the project has no line-width lint, so the line ratchet was satisfied by widening. `scripts/file_size_guard.py` now ratchets two further measures of the same files, same rule, same scope, still storing nothing: (1) AST statements -- invariant under renaming, line-joining, wrapping and re-indenting, so neither a re-layout nor a rename can move it; a file over the 400-line floor may not gain one against `origin/main`; (2) lines wider than the derived width fence -- a file may not gain one by identity (path + the line's text), so a widened line fails and passes once wrapped, while the trunk's ~416 pre-existing wide lines pass (the fence is DERIVED at check time as the 99.9th percentile of line widths over both trees, tighter wins; it resolved to 120 on 2026-10-04 and again on 2026-10-05). Run against the two changes it was built for, it names 25 and 30 new wide lines and +2,367 / +2,320 / +1,378 non-whitespace characters in files that "shrank". Tests: `test_a_line_widened_past_the_limit_fails_and_passes_once_wrapped`, `test_more_statements_in_fewer_lines_is_still_growth`, `test_a_pre_existing_wide_line_is_not_reported`.

**The character count was the eighth proxy guard to misfire (2026-10-05).** It counted spelling, and a boundary is drawn by renaming: moving a method off the giant pipeline class onto a real collaborator rewrites every call site from `self.foo(...)` to `self.admission.foo(...)` -- ten more characters, zero new behaviour, zero new statements -- and the ratchet reddened five test files at +10 to +81 characters each, with seven more conversions of the same shape queued. The only green route was to keep a delegating shim, so the guard was manufacturing the cosmetic-split anti-pattern it exists to prevent. Measure (1) is now an AST statement count. Measured on `origin/main` the day of the change: 1,201 tracked `.py` files, all parseable, 280 of them over the 400-line floor holding 107,157 statements between them; the largest file is 4,524 lines and 1,894 statements. Nothing is stored -- the trunk side is parsed at check time, exactly as the line and width measures are. Semicolon joining, the route that killed the original line-only rule, buys nothing: `a = 1; b = 2` parses as two statements (measured), and the companion statement-cram ratchet refuses the shape outright. The NAMED RESIDUAL HOLE, written into the guard's own docstring: a statement count errs PERMISSIVE on expression growth -- a four-statement loop collapses into a one-statement comprehension (measured 4 -> 1), and chained ternaries, `lambda` and the walrus launder the same way. What binds instead is the width fence (a laundered expression is long, and a new line past the fence fails) and the line ratchet (the saved statements cannot be spent on new lines). A file that no longer parses is reported, never waved through.

**An import is not growth (2026-10-05, ninth proxy misfire).** The same guard refused the opposite half of a split. Lifting a helper out of a widely-used module adds one `import` line to each call site; when those sites are themselves over the 400-line floor -- 279 of 1,213 tracked `.py` files, measured -- the extraction reddens every one of them, in lines AND in statements, so the only green route is to leave a forwarding shim on the module being emptied: the cosmetic split the owner rejected, manufactured by the guard for the second distinct reason in one week. Reproduced before changing anything, by adding one `from ... import ...` line and nothing else to four real over-floor files (`earnings_analyst`, `evening_analyst`, `macro_analyst`, `news_analyst`): eight violations, four line and four statement, each `+1`, with no other delta in the tree. `scripts/file_size_guard.py` now excludes imports from both measures. THE MECHANISM, not the inconvenience: the ratchet refuses more CODE, and `ast.Import`/`ast.ImportFrom` carry only module names and aliases -- the grammar admits no expression, call or assignment inside one -- so no behaviour can be expressed in an import and none can be smuggled past by being spelled as a dependency. The exemption is by AST identity, never by what the author calls the change; there is no allow-list, no per-file table and no raised number. The line measure drops only lines holding an import AND NOTHING ELSE (`import_only_lines`), and the proxy errs STRICT: a multi-line parenthesised import and `from x import *` are exempt in full; an import inside an `if`/`try`/`def` is exempt on its own line while the block header it needs is charged in lines and statements; `import mod; mod.run()` is charged in full, line and statement. The width fence is NOT relaxed -- a new over-wide import line still fails and is wrapped. It still bites: a file gaining one real statement is still refused (`test_the_rule_still_bites_on_one_real_statement`, green only while the rule exists), and the ceiling population is measured in the same code-line unit, which can only lower it.
