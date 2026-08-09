# Safety Boundaries

1. Paper trading only; live trading is not authorized.
2. Deterministic Python risk and broker-side protection are final authority.
3. AI Risk Manager is an advisory/challenge layer, not a replacement for hard rules.
4. Risk-system uncertainty/failure must fail closed.
5. Dashboard/API/AgentLens failure cannot remove stops or other broker-side protection.
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
