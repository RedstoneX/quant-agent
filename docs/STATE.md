# QAMC Current State

Updated: 2026-08-20

This file records what is accepted and true **now**. Git history preserves prior implementation detail; do not turn this file into a changelog.

## Accepted production state

- **Terminology:** **QAMC / Mission Control** means the whole product/system. **Dashboard** means the browser/iPad read-side UI and its frontend/UX workstream. **Core recovery** means trading/backend deployment and natural-validation work.
- QAMC is an autonomous AI-assisted Alpaca trading system whose **currently authorized execution environment is Alpaca Paper**. Live-broker order submission, margin, options and direct stock shorting are not authorized.
- Paper vs live is an execution-environment boundary, not a separate trading architecture. The accepted Specialist → PM → AI Risk → deterministic risk/execution → position-management path is intended to remain the same if live-capital operation is later authorized; genuine environment differences stay at the broker/configuration boundary.
- Production runtime is owned by `qamc`; administration/recovery by `ubuntu`; development/Claude Code by `dev`. These account boundaries remain hard.
- Dashboard/API remain private, read-only and non-critical to trading. `/cockpit`, `/ui` and `/health` are deployed and healthy.
- Private operator access uses Tailscale. Canonical VPS MagicDNS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.
- OneCLI remains the accepted credential-delivery layer. Docker publishes OneCLI only on loopback (`127.0.0.1:10254-10255`); the dashboard may also be reached through `tailscaled` on this host's exact tailnet addresses. The credential gateway itself remains loopback-only. No public listener is authorized.
- The current Alpaca Paper validation run remains active under the existing seven `qamc` user timers.

## Production code position

Production is pinned at `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` (tree `7a795888f7794bbd7049ecd5468bf0aa3f419d86`), deployed 2026-08-20 23:58:56 UTC (PR #56, the trading-utility recovery, merged to `main`). The checkout is intentionally detached at that exact SHA rather than following `main`.

The checkout carries exactly **one** intended local delta: `config/settings.yaml`, `intraday_scan.enabled: false -> true`. That is the authorized intraday enablement and the only production-vs-commit difference observed at Gate E.

Rollback point is the immediately prior production SHA, `775296e1d516279381a4c516dfb3e783b33a7495` (`775296e1`). The accepted rollout transcript is `/root/qamc-rollout-20260820T235856Z.log` on the VPS (root-only); the deployment script and its full defect history are `ops/review/qamc-recovery-rollout.sh` and `ops/review/README-recovery-rollout.md` on branch `claude/trading-utility-recovery-rollout`.

## Trading-utility recovery — merged, deployment pending

PR **#56 — `fix(qamc): restore trading-utility conversion path`** merged into `main` on 2026-08-20. Merge commit: `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`; reviewed recovery head: `04f6f76a65f7c02891449a243320977695523117`.

The merged recovery repairs evidenced opportunity→decision→execution blockers including PM parse-fragment selection, SGOV funding latency, bounded fail-closed PM/Risk schema repair, execution-skip observability, FRED/staleness conservatism and deterministic sizing provenance to AI Risk.

**Production has not been declared converged to this merge.** The deployed SHA above remains authoritative until governed rollout evidence records a new production pin.

## Finish-line acceptance — complete

The governed Gate A→E rollout completed successfully in one guarded run and ended with `GATE E / FINISH LINE PASSED`, independently corroborated live from `dev` (`/health` reachable, `status=ok`, `paper=true`, `/cockpit` and `/ui` both 200) immediately after.

Accepted evidence from that run:

- exact target SHA/tree and reviewed 30-file production delta verified before checkout, including all seven trading-utility recovery fix markers and the inherited PR #48 markers;
- production import/config smoke passed with `paper=True`, SGOV sweep enabled, the four approved inverse ETFs present, and intraday still OFF at cutover;
- Dashboard/API restarted healthy on the target;
- commissioning verifier: 23/23 checks PASS across config, OneCLI, wiring, providers and Dashboard/API;
- live provider preflight: 9/9 checks PASS, including both accepted OpenRouter models, Alpaca Paper account/market-data/calendar/quote paths and FRED;
- Telegram `getMe` returned 200 through OneCLI; the real bot token remained only in OneCLI and no token-shaped string was found in the runtime log;
- Gate C focused deterministic suite: **163 passed**, 0 failed/error/skipped/xfailed (reviewed full recovery suite: 1997 passed, 0 failed);
- SGOV live reconciliation: raw cash $144.92, parked $9858.31, reserve $100.03, backed by one real SGOV position row, zero non-sweep risk positions;
- seven existing timers remained active and unchanged, with zero failed units;
- `/cockpit`, `/ui` and `/health` all returned 200 and Dashboard/API rejected POST/PUT/DELETE/PATCH writes;
- `dev` / `qamc` / `ubuntu` account boundaries remained intact.

No order was placed, cancelled or modified by the rollout.

## Trading-utility recovery (PR #56) — deployed and verified

All seven reviewed recovery fixes were confirmed present in the deployed tree three times (pre-checkout content verification, post-checkout re-check, post-enablement final check):

1. **PM decision parsing** — nested `targets` fragments can no longer outscore and replace the complete PM decision.
2. **SGOV funding race** — funding SELL confirmation now waits/polls within a bounded fail-closed window before dependent BUYs proceed.
3. **Schema-complete decisions** — a bounded repair is allowed only for non-decision narrative fields; decision-bearing content is preserved or the run fails closed.
4. **Execution-skip telemetry** — deterministic BUY skips are durable evidence rather than log-only.
5. **Unfunded approved-BUY retry** — fully unfunded approved morning runs return a safely retryable status through the full decision chain rather than being silently lost.
6. **Macro conservatism** — FRED gets bounded retry/breaker behavior; staleness follows each series' actual publication cadence.
7. **Constructor sizing-cap provenance** — deterministic risk-budget caps carry an explicit note so AI Risk does not mistake them for PM inconsistency.

PR #48's three fixes (SGOV funding semantics, Tech batch completeness, intraday discovery) are inherited in this tree and were re-verified, not merely assumed carried forward.

Deployment passing proves the machinery is wired correctly. It does not by itself prove the recovery works — that requires natural Alpaca Paper market evidence; see Handoff below.

## Intraday opportunity discovery — enabled

`intraday_scan.enabled: true`, using the existing `quant-agent-intra_check.service` scheduled by `quant-agent-intra_check.timer`. No timer, service, daemon or other durable component was added. This override has now survived one full redeployment (finish-line → trading-utility recovery), re-established by the deployment script's governed Gate D rather than assumed to persist.

Accepted live configuration:

- move threshold: **3.0%** absolute move from previous close;
- per-symbol cooldown: **3.0 hours**;
- maximum candidates per tick: **5**;
- approved bearish-expression ETFs present: `SH`, `SDS`, `PSQ`, `SQQQ`.

Before enablement, the scanner's own Alpaca snapshot path returned usable previous/current SPY pricing in a read-only smoke test. The same Specialist → Portfolio Manager → AI Risk Manager → deterministic Python/broker chain remains authoritative. Existing process/session locks, cooldown, candidate cap and current-session incomplete-data labeling remain intact.

The first naturally scheduled Tech batch line and first naturally scheduled enabled intraday tick are validation observations, not acceptance gates. They must not be manufactured by forcing a trade or session.

## Accepted decision/model policy

- Decision chain remains: Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker.
- Deterministic Python and broker protections remain final safety authority.
- OpenRouter remains the model-provider path.
- Current accepted routing uses two model IDs across the nine seats: `google/gemini-2.5-flash-lite` and `qwen/qwen3-235b-a22b-2507` according to the per-seat policy.
- Cost-optimized routing and the accepted decision-chain audit remain in force.
- Trading-critical behavior is environment-neutral by design; Paper mode must not justify weaker or alternate agent/risk/position-management semantics.

## Dashboard product direction

Dashboard product convergence is authorized to proceed concurrently with the trading-utility deployment/validation so long as it remains private, read-only and non-critical to trading.

`docs/visual/MISSION_CONTROL_VISION_BOARD.png` remains the durable product reference. The accepted direction includes the donor concepts recorded in `docs/OUTCOME.md`, especially Oralexa-style agent cards/debate/signal fusion/PM-Risk decision presentation, OpenTradex-style cockpit shell/layout ideas, TradingView Lightweight Charts-style chart context, and the structured Journal Day experience. Later semantic audits refine these ideas; they do not silently discard them.

## Directionality

- QAMC is not intended to be structurally long-only.
- Bearish expression remains through approved inverse ETFs (`SH`, `SDS`, `PSQ`, `SQQQ`).
- Direct stock shorting, options/theta strategies and margin remain outside the accepted architecture.

## Not authorized

- Live-broker order submission or live-capital activation.
- Direct stock shorting, options/theta strategies, or margin.
- New timers, daemons, services, databases, proxies, credential systems or other durable infrastructure outside accepted architecture.
- Deterministic risk/execution semantic redesign.
- Paper-specific shortcuts or a separate Paper-only trading logic path that would require re-architecting for live operation later.
- Broker-write Dashboard controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing/security/credential platform without an accepted architectural decision.
- Forcing/manufacturing a trade merely to prove behavior.

## Handoff

The trading-utility recovery (PR #56) is deployed, verified and accepted as *machinery* — production is running the exact reviewed SHA, all seven fixes are confirmed present and wired, and every existing safety/observability gate passed. It is **not yet accepted as a working recovery**: that requires natural Alpaca Paper market sessions demonstrating the actual goal in `docs/WORK.md` — opportunity discovered → evaluated → a defensible decision → executed when eligible → managed/exited → measured — including defensible no-trade outcomes. Do not force, manufacture or accelerate that evidence. Dashboard product convergence continues concurrently on an isolated branch/worktree under `docs/WORK.md`, without altering or delaying the trading-critical validation path. See `docs/WORK.md`.
