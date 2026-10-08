# Rebuild all oversized files — plan of record

Mandate (owner, 2026-10-01): **"EVERYTHING needs to be done NOW - properly."**
Every file in `src/` over the 2,561-line ceiling gets under it. Split what can
be split; rebuild the broker connection and the database layer behind their
interfaces rather than splitting them. Behaviour does not change while this
runs: bugs noticed in passing go to `docs/SPLIT_DEFERRED_FINDINGS.md`.

## Where it stands

Started 2026-10-01 at **16 files, 26,951 lines above the ceiling**.

| File | Start | Now | State |
|---|---|---|---|
| `src/execution/broker.py` | 7,046 | 1,710 | rebuilt in four instalments |
| `src/storage/db.py` | 6,270 | 2,431 | rebuilt in three instalments |
| `src/pipeline_protection.py` | 4,438 | 2,420 | split |
| `src/agents/portfolio_manager.py` | 4,034 | package | split |
| `src/pipeline.py` | 4,993 | 2,529 | split |
| `src/pipeline_exits.py` | 3,754 | 2,454 | split |
| `src/config.py` | 2,610 | package | split |

The other nine went under the ceiling earlier in the same drive.

Re-measure, never quote this table from memory:

```
git ls-tree -r --name-only origin/main src | grep '\.py$' |
  while read f; do n=$(git show origin/main:$f | wc -l);
  [ "$n" -gt 2561 ] && echo "$n $f"; done | sort -rn
```

## The doctrine that made it work

- **Code moves verbatim.** Prove each moved body byte-identical to its
  pre-move source; asserting it is not proving it. Three reports claimed a
  clean move that measurement contradicted.
- **A piece is a module only if it can be built and exercised without a
  `TradingPipeline`.** `tests/boundary_harness.py::check_boundary` is the
  test, and every new piece ships a witness test that calls it. Mixins are
  not boundaries — a whole earlier round of "splitting" was cosmetic for this
  reason.
- **Standalone class, keyword-only collaborators, thin same-named shim.** The
  shim builds its object **per call**, so a collaborator swapped after
  construction is what the body sees.
- **Never pass a collaborator that is itself one of the lifted methods.** The
  shim would overwrite the new object's own method with a function that calls
  straight back into it — infinite recursion. This cost two full runs on the
  broker. The guard in `src/execution/broker.py` is the reference, and it has
  to see through a `functools.partial`, because tests bind bodies onto mock
  hosts that way.
- **Build a package, never siblings.** the `broker-seam` layer rule (`LAYER_RULES` in
  `scripts/import_graph.py`) matches importers by prefix, so `src/x/y.py` stays inside `src/x`'s existing
  allowance while `src/x_y.py` does not.
- **(Retired 2026-10-08: the size ratchet was replaced by fixed ruff limits in
  `pyproject.toml`.)** `FLOOR = 400` in `scripts/file_size_guard.py` was the cap on a file that is
  not on `origin/main`. Files already on the trunk are judged only by whether
  they GREW against it; there is no recorded baseline to edit any more, and
  nothing to regenerate.
- **Baselines may only shrink for existing keys.** Verify in Python against
  `origin/main` before every push. A branch cut before another landed will
  otherwise hand back every line that one removed.
- **Grep every lifted body for `getattr(self, ..., default)`** — it silently
  becomes `None` on the new object. A real near-miss on a money path.
- **Re-point, never relax.** A test that inspects source, or a ledger row that
  cites a file and line, follows the body to its new home with its assertions
  and its value, source and status unchanged.
- **A guard refusing a change is probably telling the truth** — read its
  source before concluding it blocks you.
- **Check the one load-bearing claim of every report.** Roughly one in three
  was wrong, including two that reported the opposite of what the code said.

## What is deliberately left

- A ~180-line sector-lookup cluster stays in the broker: it is not needed to
  clear the ceiling and carries 94 patch targets.
- `place_entry_protection` stays on the broker; moving it would close an
  import cycle through the scale-in path.
- The composition root of `src/pipeline.py` (its `__init__`) is its own piece
  of work, after the ceiling.
- Real boundaries, not just smaller files, are still owed by the position
  builder, the portfolio-manager seat and the prompt-facts review chunk (the
  cost circuit is done: all eleven parts are held instances, no mixin
  remains, 2026-10-02).

## Appendix — measured inventory taken before the two rebuilds


## Broker (src/execution/broker.py)

**File size and complexity:**

| Metric | Count |
|--------|-------|
| Total lines | 7047 |
| Classes | 8 |
| Test files importing | 44 |

**Top 10 methods/functions by line count:**

| Method | Lines |
|--------|-------|
| AlpacaBroker.submit_order | 325 |
| AlpacaBroker.replace_stop_loss | 270 |
| AlpacaBroker.place_entry_protection | 261 |
| AlpacaBroker.shift_stops_down | 173 |
| AlpacaBroker._amend_resting_stop_price | 169 |
| AlpacaBroker._submit_stop_limit_order | 160 |
| _install_trading_stream_reconnect_guard | 157 |
| AlpacaBroker.get_intraday_snapshots | 157 |
| _install_trading_stream_auth_diagnostics | 140 |
| AlpacaBroker._restore_stop_orders | 137 |

**Classes by line count:**

| Class | Lines |
|-------|-------|
| AlpacaBroker | 5191 |
| _TradeUpdatesHub | 247 |
| _TradeUpdatesLease | 67 |
| _StreamAttemptBudget | 59 |
| LivePrice | 32 |
| TradeStreamAuthRejected | 41 |
| TradeStreamGaveUp | 9 |
| _HubWaiter | 7 |

**Public surface — top 15 referenced methods/attributes (from src/ grep):**

| Reference | Count |
|-----------|-------|
| broker.get_positions | 20 |
| broker.get_order_fill_info | 12 |
| broker.get_account | 10 |
| broker.is_trading_day | 9 |
| broker.trading_sessions_held | 8 |
| broker.submit_order | 8 |
| broker.snapshot_protective_stops | 8 |
| broker.get_current_stop_price | 8 |
| broker.wait_for_order_terminal | 7 |
| broker._restore_stop_orders | 6 |
| broker.get_latest_price | 5 |
| broker.get_bars | 5 |
| broker.get_session_close | 4 |
| broker.get_intraday_snapshots | 4 |
| broker.cancel_open_entry_orders | 4 |

**Public surface:** 48 distinct names accessed on a `broker` handle from src/ [measured by AST over every `src/**/*.py`, counting attribute access on `broker` and `*.broker`]. An earlier count of 19 in this document was wrong: it missed every access reached through an intermediate attribute, and the rebuild must honour the larger surface.

---

## Database (src/storage/db.py)

**File size and complexity:**

| Metric | Count |
|--------|-------|
| Total lines | 6271 |
| Classes | 1 |
| Test files importing | 104 |

**Top 10 methods/functions by line count:**

| Method | Lines |
|--------|-------|
| Database._migrate | 715 |
| Database.compute_trade_calibration | 382 |
| Database._create_tables | 373 |
| _assign_position_ids | 140 |
| Database.backfill_conviction_ledger | 136 |
| Database.save_evening_snapshot | 130 |
| Database.insert_stop_out_trade | 130 |
| Database.resolve_conviction_ledger | 112 |
| Database.insert_trade | 106 |
| Database.update_open_stop_loss | 100 |

**Classes by line count:**

| Class | Lines |
|-------|-------|
| Database | 5746 |

**Public surface — top 15 referenced methods (from self.db.* grep):**

| Reference | Count |
|-----------|-------|
| self.db.insert_agent_log | 12 |
| self.db.get_trades | 11 |
| self.db.get_symbol_last_buy | 11 |
| self.db.record_intraday_evaluation | 10 |
| self.db.delete_pending_protection_restore | 7 |
| self.db.insert_trade | 6 |
| self.db.insert_specialist_evidence | 6 |
| self.db.get_recent_insights | 6 |
| self.db.get_recent_agent_outputs | 6 |
| self.db.get_daily_pnl | 5 |
| self.db.update_trade_fill | 3 |
| self.db.update_pending_protection_restore_specs | 2 |
| self.db.record_intraday_symbol_snapshot_result | 2 |
| self.db.insert_pending_protection_restore | 2 |
| self.db.get_pending_protection_restores | 2 |

**Public surface:** 67 distinct names accessed on a `db` handle from src/ [measured by AST, same method as the broker above]. An earlier count of 20 was wrong for the same reason.
