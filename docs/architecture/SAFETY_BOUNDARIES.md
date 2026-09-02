# Safety Boundaries — Accepted Contract

## Non-negotiable

1. Alpaca Paper only; live trading is not authorized.
2. Deterministic Python risk and broker-side protection are final authority.
3. AI Risk Manager is advisory/challenge logic, never a replacement for hard rules.
4. Safety uncertainty/failure fails closed.
5. Mission Control/API failure must have zero effect on trading or broker protection.
6. Read-only Mission Control cannot issue broker writes or override deterministic rejection.
7. Provider/model/prompt changes cannot bypass protected hard-risk behavior.
8. Meta Reflector changes remain human-approved while protected risk behavior stays outside automatic evolution.
9. Any future writable risk configuration requires explicit authorization, server validation, bounded ranges and auditability.

## Verified caveats that matter

- `cash_sweep` `SWEEP_BUY` intentionally bypasses the shared hard-risk gate. It is deterministic, config-fixed and treated as cash-equivalent; its own bounds govern it.
- The shared deterministic gate runs before AI Risk and again after AI-applied modifications; AI cannot loosen a hard limit.
- AI Risk can widen a positive `stop_loss`; that widening is not separately re-audited for reward/risk. This is a known narrow behavior, not permission to redesign risk during unrelated work.
- `alpaca.paper` is the effective paper/live selector; `alpaca.base_url` is not a second live-safety switch.
- The accepted Stage-2 API is separate-process, GET-only/read-only, uses independent SQLite `mode=ro` history reads, and has no trading-process dependency.
- The accepted Smart Money external-symbol lane is temporary and run-scoped.
  Only deterministic SEC Form 4 open-market purchase evidence may nominate a
  symbol, and broker common-equity eligibility, price, history, liquidity and
  known-sector checks must all pass. Admission bypasses only permanent-universe
  membership and the Technical prefilter; it never bypasses current Technical
  analysis, PM grounding, AI Risk, deterministic risk/funding rules, broker
  protection or Alpaca Paper authorization. The configured universe is not
  mutated, and any uncertainty fails closed.
- `scripts/desk_reset.py` is the only operator tool that issues broker
  liquidations (`DELETE /v2/positions?cancel_orders=true`). It is outside the
  trading pipeline and outside the risk engine, so it carries its own
  fail-closed paper check rather than inheriting one: `alpaca.paper`, the
  configured `base_url`, the SDK client's **resolved** base URL, and the
  account's own `PA` account-number prefix must all say paper, and a read-only
  probe of the live host must **not** authenticate. Any single failure aborts.
  Note the qualifier above — `alpaca.base_url` is not a live-safety switch for
  the *pipeline*; inside this tool it is one of four independent signals, none
  of which is trusted alone. There is deliberately no override flag: a
  liquidation against a live account should require a reviewed code change,
  the same bar as `AppConfig._enforce_paper_only`. The tool never drops a
  table and never deletes a file; unknown tables are kept, not emptied.

Detailed verification remains in Git history. The last pre-ultra-lean working-tree snapshot is commit `02e20e6ac1c5c7e65b7f512f76c568328c990e3c`. Current authorization is always controlled by `docs/STATE.md` + `docs/WORK.md`.
