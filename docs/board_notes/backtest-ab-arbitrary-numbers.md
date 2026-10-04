# A/B-ing the ledger's `arbitrary` numbers through the deterministic backtester

Measurement only. No config default and no ledger row was changed by this note.

## Which arbitrary numbers the engine can reach at all

`config/number_ledger.yaml` carries 124 rows at `status: arbitrary` (measured
2026-10-04 against `origin/main`). `scripts/backtest.py` loads the real
`AppConfig` but reads only `config.risk` / `config.trading` / `config.execution`,
so a row is A/B-able only if it is one of those *and* is consumed by the
deterministic layer the engine exercises. Grepping `src/backtest/`,
`src/risk/budget.py`, `src/risk/trailing.py`, `src/portfolio_constructor.py`
and `src/data/levels.py` for config reads leaves this set:

| ledger row (`status: arbitrary`) | where the engine consumes it |
| --- | --- |
| `risk.max_position_risk_pct` | the §2.1 sizing formula; also the size of every `RiskRequest` the engine makes |
| `risk.max_portfolio_risk_pct` | `allocate_risk_budget` ceiling |
| `risk.max_cluster_risk_share_pct` | `allocate_risk_budget` cluster share cap |
| `risk.absolute_min_stop_atr_multiple` | `_resolve_stop` level-backed exemption floor |
| `risk.min_level_touches_for_stop_honor` | structural-level stop honouring |
| `risk.max_target_reach_atr_multiple`, `risk.max_target_horizon_sessions`, `risk.target_divergence_warn_pct` | target sanity checks only — advisory, no fill effect |
| `execution.max_entry_slippage_bps` | reused by the CLI as the flat slippage estimate on both fills |

`risk.min_stop_atr_multiple` is deliberately excluded: the owner closed it at
2.5 ATR on 2026-10-04.

Everything else in the 124 is out of reach — it lives in `SmartMoneyConfig`,
`IntradayScanConfig`, `NominationConfig`, broker retry/backoff constants,
exit-guard noise floors or pipeline factors, none of which this engine calls.

## The A/B that was run

Two copies of `config/settings.yaml` differing in exactly one line:

* **A** — `risk.max_portfolio_risk_pct: 25` (current default)
* **B** — `risk.max_portfolio_risk_pct: 15`

Window 2025-01-01 .. 2025-12-31, 20-name universe slice taken from
`trading.universe` (the large-cap and sector-ETF block), yfinance daily bars,
$100,000 starting equity.

Reported as a difference, B minus A:

| Metric | Delta (B − A) |
| --- | --- |
| Trades | −41 |
| Win rate | −1.08 pts |
| Win/loss ratio | +0.141 |
| Expectancy | +0.0215R |
| Max drawdown | −10.83 pts |
| Total return | +15.04 pts |
| **Binding-budget days** | **243 of 245 → 246 of 246** |

## This is a NON-RESULT, and that is the finding

The engine's own caveat says that on a day the risk budget binds, its
equal-size unranked `RiskRequest`s are served by the allocator's **alphabetical
ticker tie-break**, not by production's verdict ranking, and that the binding
share "is not a discount you can apply to the other numbers: who got funded
changes later equity, later size, and later outcomes."

In this run the budget bound on **essentially every entry day in both arms**
(243/245 and 246/246). There is no unrationed sub-period to read a clean signal
from. The entire +15.04-point return difference is produced by a different
alphabetical subset of tickers getting funded, compounding into different
equity and therefore different later sizes. It is not evidence that a 15%
portfolio risk ceiling is better than 25%.

Two further caveats the engine prints apply on top and are not relieved by a
longer window: survivorship bias (the universe is a present-day fixed list, no
delisted names), and the deterministic stop substitution (nearest structural
level stands in for the Tech Analyst's chart read).

## What this implies for the other reachable rows

Because the budget binds on ~100% of entry days under the shipped defaults,
the same contamination lands on **every** reachable row in the table above, not
just the two budget parameters: any change that alters how many shares a
candidate asks for, or where its stop sits, changes which alphabetically-ordered
candidates fit under the ceiling. `max_position_risk_pct`,
`absolute_min_stop_atr_multiple` and `min_level_touches_for_stop_honor` were
therefore not run — the result would have been invalidated by the same caveat
before it was read.

**Conclusion: none of the 124 arbitrary rows can currently be settled by this
engine.** The blocker is not the engine's scope, it is that its unranked
equal-ask design makes the budget bind on every entry day. The prerequisite for
any future A/B here is a deterministic, non-alphabetical ordering for the
engine's `RiskRequest`s — or a run configuration in which the budget does not
bind — and neither is invented here.
