# Broker and database rebuild — measured inventory (2026-10-02)

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
