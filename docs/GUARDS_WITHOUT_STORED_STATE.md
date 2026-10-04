# Guards that store nothing — the plan of record

Owner mandate, 2026-10-02: "Yes, rip it all out. Rip all out the stored
bookkeeping. And rebuild it properly so it's not a ticking time bomb." This is
the specification. Nothing else is started until it is built and proven.

## Why, measured

A read-only sweep of all 19 then-open changes, 2026-10-02:

| Finding | Measured |
|---|---|
| Red changes failing on shared bookkeeping, not their own code | 8 of 11 (73%) |
| Tracked files sitting at EXACTLY their recorded size cap | 291 of 292 |
| That day's commits touching the one shared 293-entry baseline | 48% |
| Test files reading source TEXT/AST/paths rather than running code | 154 of 384 (40%) |

`test_baseline_is_tight` makes leftover headroom a FAILURE, so headroom is
illegal by construction. Adding one line anywhere in the tree therefore forces
an edit to a single shared file that every other open change is also editing.
That is not bad luck; it is arithmetic, and it is why changes jam all day.

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
| ~~`tests/silent_swallow_baseline.json`~~ | DONE — deleted; `scripts/silent_swallow_guard.py` now names each silent-swallow site in the money modules in the working tree and on `origin/main` via `scripts/guard_reference.py` and fails on any new site identity (never on a total, so a swap of one offender for another still fails) |

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
- ~~the unscoped-number ceiling in the number-sources guard~~ DONE — `MAX_UNSCOPED_NUMERIC_SITES` and its pinning test are deleted; `scripts/unscoped_number_guard.py` runs the unscoped scan on the working tree and on `origin/main` at check time and reports the delta

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

## Acceptance — proven, not asserted

1. **Two unrelated changes at the same time never collide.** Branch twice off
   main, add a line to a different file in each, and merge both. This must
   succeed with no conflict. Today it cannot, because both edit the baseline.
   This is the test the owner was promised.
2. **Each converted guard still catches what it caught.** Before deleting a
   baseline, record what its guard currently flags; after conversion, the same
   change must still be flagged. A guard that goes quiet has been deleted, not
   converted.
3. **Every converted guard REFUSES when `origin/main` is unreachable.** Test it
   by pointing the remote at nothing.
4. **No tracked file sits at exactly its cap afterwards, because no cap is
   recorded.** The 291-of-292 number should become meaningless.

## Order

Convert ONE guard end to end first — the size ratchet, which causes most of the
pain — and prove all four acceptance criteria on it before touching the others.
A half-converted set is worse than either the old one or the new one.

Also done: ~~`tests/pipeline_method_inventory.json`~~ (a script-regenerated measurement, not policy) is deleted with its `--write` mode; `scripts/pipeline_method_guard.py` names each owner of a method defined on 2+ of `TradingPipeline` and its mixins, in the working tree and on `origin/main`, and fails any new identity. A move that leaves one copy never fails.

Also done (2026-10-02, Python-clothed baselines): ~~`TRADING_PIPELINE_TEST_FILE_BASELINE = 79`~~ in `tests/test_boundary_harness.py` (a count measured on 2026-10-01, a TOTAL) is deleted; `tests/boundary_harness.py::trunk_test_files_referencing_pipeline` now names each test file that names `TradingPipeline` in the working tree and on `origin/main` at check time and fails on any new path. The skip of the harness's own files, the composition root and the one deliberate whole-system test is unchanged and is policy, not a measurement. ~~`_WORK_MD_OVERSIZE_ON_ARRIVAL`~~ / ~~`_WORK_MD_POINTERLESS_ON_ARRIVAL`~~ in `tests/test_status_board.py` (eight item sizes and four item numbers measured on 2026-10-01) are deleted; `scripts/board_item_guard.py` names each (item, rule) offence -- over the per-item budget, or without a resolving note pointer -- on the working tree's board and on `origin/main`'s, and fails on any new identity. The budget's divisor (40 open items) stays: it is a chosen policy figure, not a measurement. One consequence stated plainly: an item that is already over budget on the trunk may be edited, including grown, without failing -- pre-existing rot is the trunk's, not the change's, exactly as `board_rot_guard` treats finished-but-open items. Both refuse without `origin/main`; removals never fail.

## Weakening a guard needs a written reason

`tests/test_guard_weakening_gate.py` (logic in `scripts/guard_weakening_gate.py`) fails any change that edits or deletes an existing guard file without a one-line `Guard-rule-change:` of 25+ words in a commit message. Guard files are derived by naming rule (`scripts/*guard*.py`, `tests/test_*guard*.py`, `tests/test_*ratchet*.py`), never listed. Tightening cannot be told from loosening, so every behavioural edit is asked; only new guard files and docstring/comment/format-only edits (identical AST) are exempt. An unreadable base is a failure.

Also done (2026-10-04, the settlement-recording class): `scripts/settlement_fill_guard.py` refuses a NEW site that hands a `built` settlement route's field to the writer through a three-argument `getattr` or a literal `None`. It stores no list of known offenders -- it names each offending (file, enclosing scope, field, shape) in the working tree, names them again on `origin/main`, and fails only on an identity the tree holds that the trunk does not; it refuses outright when the trunk cannot be read. This is the layer ABOVE the one `tests/test_settlement_recording_writes.py` checks: that test reads the storage layer's AST and asks whether anything writes the column, and it answered YES, correctly, for all four recordings that nonetheless recorded nothing. The defect was always the caller's expression, and a defaulted `getattr` is the one expression that cannot fail. What this guard deliberately does NOT decide is whether a write is CENSORED by the branch it sits on (the noise band, PR #1177, wrote only where the band blocked an exit); that is a question about the meaning of a condition, not its shape, and what settles it is a production measurement that the accrued sample holds observations on both sides of the threshold.

## Compressing is the same offence as growing

`tests/test_statement_cram_ratchet.py` (logic in `scripts/statement_cram_guard.py`) closes the route a change took on 2026-10-04 to satisfy the size ratchet without splitting anything: it joined statements onto shared lines (`from A import x; from B import y`, `if cond: return x`) and only the line counter moved. The guard PARSES every tracked `.py` file (the size ratchet's own scope, `working_paths("*.py")`, no second list) and names each line on which more than one statement starts, or whose block body sits on its header's line (`if`/`elif`/`except`/`else`/`finally`/`case` headers alike); semicolons inside strings, docstrings and comments are invisible to it. There is no threshold -- the measure is statements per line -- and no stored list: identities (`path`, enclosing scope, the line's text) are collected on the working tree and on `origin/main` at check time and only a NEW or more-frequent identity fails. A one-line stub body (`class Boom(Exception): pass`, `def f(self) -> int: ...`) is not cramming and is exempt. It refuses without `origin/main`; removals never fail; the ~116 pre-existing crammed lines on the trunk pass (measured 2026-10-04).
