# QAMC Current Work

Status: **FINISH LINE REACHED — PAPER SOAK RUNNING ON THE ACCEPTED TARGET**

The stage-gated finish-line rollout is complete. Production is pinned at `775296e1d516279381a4c516dfb3e783b33a7495` with the single authorized local config delta `intraday_scan.enabled: true`. Stage E acceptance passed in the same guarded run. See `docs/STATE.md` for the accepted production state and rollback point.

## Current work — observe the soak

Do not manufacture activity. Let the existing seven-timer paper schedule produce evidence naturally.

Immediate observations to capture:

1. the first live Tech batch line (`Batch: N/M symbols analyzed`) from a scheduled research run;
2. the first live enabled `intra_check` tick during the regular 09:30–16:00 ET window;
3. across subsequent ticks, how often symbols qualify, what cooldown/candidate-cap guards suppress, whether candidates reach PM/Risk, and the incremental model/market-data cost.

These are **observations, not deployment gates**. A lack of qualifying trade candidates is not itself a defect. If the evidence shows an unexpected shape, investigate within the accepted architecture; do not force a trade or widen trading authority to create activity.

## Accepted baseline for soak review

- Alpaca Paper only.
- Intraday threshold 3.0%, per-symbol cooldown 3.0h, cap 5 candidates per tick.
- Existing `quant-agent-intra_check.service` / `quant-agent-intra_check.timer`; no new scheduler or daemon.
- Approved bearish expression through `SH`, `SDS`, `PSQ`, `SQQQ` only.
- Specialists → Portfolio Manager → AI Risk Manager → deterministic Python/broker gate remains the decision chain.
- SGOV remains deterministic cash-equivalent sweep parking.
- Mission Control remains private and GET-only; Telegram remains output-only.

## Bounded, non-blocking product debt

The read-only Mission Control liquidity API currently uses `liquidity.deployable_cash` for **raw broker cash above the reserve floor**, while `total_liquidity` carries raw cash plus sweep-parked value and the trading engine can fund buys from convertible SGOV. The values are not currently wrong, but the field name can cause an operator to read “cash immediately free” as “total capital QAMC can put to work.”

Do not change that field's meaning in place during the soak. A clean correction should be a coordinated read-only API/schema + cockpit change (for example, rename the raw-cash field and expose an explicit engine-deployable figure), followed by frontend rebuild and browser verification.

## Next product tranche — after initial soak evidence

1. Review several live enabled intraday ticks and Tech batches: qualification frequency, candidate flow, guard suppression and added cost.
2. Then address the liquidity API naming/presentation issue together with the Mission Control cockpit so the UI answers the operator's actual question: **how much capital can QAMC put to work, and why is it currently parked or not deployed?**
3. Continue the broader Mission Control product/visual review only against truthful live data; do not turn the cockpit into a write/control surface.

## Hard boundaries

- Alpaca **Paper only**.
- No margin, options or direct stock shorting.
- Bearish expression remains through the approved inverse ETFs.
- Deterministic Python/broker protections remain final.
- No broker-write Mission Control controls.
- Telegram remains output-only; no command/control plane.
- No new daemon/service/database/proxy/security/credential architecture without a new architectural decision.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Do not expose QAMC or OneCLI publicly.
- Do not weaken OneCLI secret handling or put real provider/Telegram credentials into repository/runtime logs.
- Do not force or manufacture paper trades for verification.
- Claude does not merge its own work; GitHub integration remains externally reviewed.
