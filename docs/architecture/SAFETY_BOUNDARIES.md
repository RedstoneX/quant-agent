# Safety Boundaries

1. Paper trading only; live trading is not authorized.
2. Deterministic Python risk and broker-side protection are final authority.
3. AI Risk Manager is an advisory/challenge layer, not a replacement for hard rules.
4. Risk-system uncertainty/failure must fail closed.
5. Dashboard/API failure cannot remove stops or other broker-side protection.
6. Mission Control cannot issue broker orders directly or override deterministic rejection.
7. Provider/model/prompt changes cannot alter protected hard-risk code/ceilings.
8. Meta Reflector proposals require human approval initially; protected risk agents/behavior stay outside automatic evolution.
9. Any later writable risk configuration must be server-validated, range-limited and audited; protected ceilings remain non-editable.
10. Emergency controls must map to real quant-agent/Alpaca lifecycle semantics discovered during implementation, not frontend-invented behavior.

## Stage 0 verification note (2026-08-09)

Boundaries 1–8 were checked against source and hold as written, with one
carve-out that must be stated rather than left implicit (discrepancy **D-9**):

**`cash_sweep`'s `SWEEP_BUY` reaches the broker outside the shared hard-risk
gate.** `src/execution/cash_sweep.py` parks idle cash in a config-fixed T-bill
ETF without passing `RiskRuleEngine.check()` / `_filter_hard_risk_decisions`,
and submits deliberately stopless. This is coherent — the vehicle is treated as
cash-equivalent everywhere (hidden from every LLM view, credited to cash in the
cash-only rule, exempt from stop-coverage audits, liquidated first by
`_force_delever`) — and it is **deterministic and not LLM-reachable**. But it
means "no BUY reaches the broker without passing the hard risk filter" is false
as written. The accurate statement: `SWEEP_BUY` is governed by its own
deterministic bounds (`enabled`, `reserve_pct`, `min_order_usd`, fixed symbol)
rather than by the shared gate.

Two supporting observations from the same pass:
- The deterministic gate runs **twice** — before the AI Risk Manager, and again
  after any AI-applied modification or `scale_all_buys`. AI cannot loosen a
  hard limit.
- Within that, `_apply_risk_modifications` permits the AI Risk Manager to widen
  a `stop_loss`, and a widened stop is not re-audited for R/R. `require_stop_loss`
  still enforces `stop_loss > 0`. Narrow, and not a hard-control bypass.

Also note **D-5**: `alpaca.base_url` is dead configuration — only
`alpaca.paper` selects paper vs. live. Boundary 1 currently rests on one flag,
not two.

## Stage 2 verification note (2026-08-09)

Boundaries 5 and 6 (dashboard/API cannot remove protection or issue orders)
now have a concrete implementation to verify against:
`src/api/` (see `docs/architecture/MISSION_CONTROL_API.md`).

- **Boundary 6 (cannot issue broker orders directly)** — verified. Every
  route registered anywhere under `src/api/` is GET-only, enforced twice
  (each router only defines `@router.get(...)`, plus an app-level
  `GetOnlyMiddleware` rejecting any other method with 405 before a handler
  runs). `AlpacaBroker` is constructed in exactly one place
  (`src/api/broker_reads.py::_get_broker`), and only its two pre-existing
  read-only methods (`get_account`, `get_positions`) plus a new read-only
  `client.get_orders(...)` call are ever invoked from `src/api/`. No
  write-capable broker method (`submit_order`, `cancel_*`,
  `close_position`, `place_entry_protection`, …) is referenced anywhere in
  the package — checked at the AST level, not by substring search, in
  `tests/test_api_safety.py`.
- **Boundary 5 (dashboard/API failure cannot remove stops or other
  broker-side protection)** — verified by construction: `src/api/` never
  calls any stop-management method, so it has no mechanism to remove
  protection in the first place, and its own failure/death is proven not to
  affect trading (`tests/test_api_isolation.py` starts the API as a real
  separate OS process, kills it, and confirms an ordinary trading DB write
  succeeds identically before and after).

`src/api/db_reads.py` opens its own independent `mode=ro` SQLite connection
rather than sharing `src.storage.db.Database`'s writer connection/lock —
verified both structurally (AST scan confirms every `conn.execute(...)`
call is `SELECT`/`PRAGMA`, no `.commit()` exists in the file) and under
load (`tests/test_api_db_concurrency.py` runs concurrent trading writes
against real API reads on the same WAL-mode file and confirms zero writer
lock errors and a passing `PRAGMA integrity_check`).
