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
| `tests/import_cycle_baseline.json` | `test_import_layering`, `scripts/import_graph` |
| `tests/import_layers.json` | `scripts/import_graph` |
| ~~`tests/pipeline_new_baseline.json`~~ | DONE — deleted; `scripts/pipeline_new_guard.py` now names each `TradingPipeline.__new__` site in the working tree and on `origin/main` at check time and fails on any new site identity |
| `tests/silent_swallow_baseline.json` | `test_silent_swallow_guard`, `scripts/silent_swallow_guard` |

All five are also read by `tests/test_baseline_merge_driver.py` and
`scripts/resolve_baseline_conflict.py` — both exist ONLY to manage collisions
between stored baselines, so both are deleted outright. A merge driver for a
file that no longer exists is the clearest possible sign the file should not
have existed.

Same class, same treatment, after the five land:
- ~~the known-leaks list in `tests/test_no_silent_patch_targets.py`~~ DONE — no list; the patch-target audit runs over the working tree and over `origin/main` at check time and fails on any unreachable-call-site identity that is new
- `_KNOWN_CHECKBOX_FINISHED_ITEMS_2026_09_26` in `tests/test_status_board.py`
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
