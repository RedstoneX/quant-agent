# QAMC Current State

Updated: 2026-08-19

This file records what is accepted and true **now**. Git history preserves prior implementation detail; do not turn this file into a changelog.

## Accepted production state

- QAMC remains an **Alpaca Paper-only** trading experiment. Live trading, margin, options and direct stock shorting are not authorized.
- Production runtime is owned by `qamc`; administration/recovery by `ubuntu`; development/Claude Code by `dev`. These account boundaries remain hard.
- Mission Control/API remain private, read-only and non-critical to trading. `/cockpit` and `/ui` are deployed and healthy.
- Private operator access uses Tailscale. Canonical VPS MagicDNS FQDN: `ovh-vps.wallaby-bowfin.ts.net`.
- OneCLI remains the accepted credential-delivery layer, loopback-only on ports 10254/10255.
- The Alpaca Paper soak remains active under the existing seven `qamc` user timers.

## Production code position

Production is currently pinned at `9c736c158fec84129765c25a9429254d3602ad6b` (`9c736c1`). That production hotfix contains the reviewed Telegram restoration/security diff on top of the prior Mission Control production baseline and deliberately excludes PR #48.

GitHub `main` contains the accepted PR #48 fixes, the Telegram restoration, and current governance/documentation. The finish-line rollout contract in `docs/WORK.md` now authorizes production convergence to a **pinned, verified exact `main` SHA** after preflight; Claude must not deploy a moving branch tip blindly.

## Telegram notification restoration — complete

The bounded Telegram restoration tranche is closed and active in production.

- Notifier request failures redact the bot token before logging.
- OneCLI 1.45.0 stores the real Telegram bot token and injects it into `api.telegram.org` requests with `pathTemplate: /bot{value}`.
- The real bot token is stored only in OneCLI; `qamc/.env` contains only a harmless placeholder plus `TELEGRAM_CHAT_ID`.
- Preflight `getMe` returned HTTP 200 before activation.
- Exactly one non-trading Telegram delivery probe returned `DELIVERED`.
- Existing scheduled wrappers were verified to source the same runtime `.env` path.
- Telegram remains notification/status output only. Commands, callbacks, webhooks and broker-write controls are not authorized.

## PR #48 — accepted, rollout authorized

PR #48 (`fix/sgov-liquidity-intraday-batch`) is merged and accepted. Its production rollout is now authorized under the stage-gated finish-line contract in `docs/WORK.md`.

It contains three accepted changes:

1. **SGOV funding semantics** — deployable cash uses owned raw cash plus convertible sweep value, while `CashSweeper.fund_buys()` reports only confirmed broker-cash increase after the sweep sale; execution's final raw-cash recheck remains authoritative.
2. **Intraday opportunity discovery** — bounded discovery on the existing `intra_check` cadence, including explicit current-session incomplete evidence and existing inverse ETFs for bearish expression.
3. **Tech batch-response completeness** — every submitted symbol reaches an explicit terminal outcome; missing batch results receive one bounded retry instead of silently disappearing.

The intraday scan is still disabled at the start of the tranche. The operator has now explicitly authorized enabling it **after** the preceding deployment and behavior-verification gates pass. No new timer/service/daemon is authorized.

## Directionality and paper-soak evidence

- QAMC is not intended to be structurally long-only.
- Approved bearish expression remains through inverse ETFs already in the universe (`SH`, `SDS`, `PSQ`, `SQQQ`).
- Direct stock shorting, options/theta strategies and margin remain outside the accepted architecture.
- SGOV is deterministic cash-equivalent sweep parking, not a Portfolio Manager investment thesis.
- The Aug 17–18 forensic found no suppressed bearish signal: sampled technical evidence did not produce a qualified inverse-ETF setup, so no prompt/intelligence correction was made.

## Accepted decision/model policy

- Decision chain remains: Specialists → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution → broker.
- Deterministic Python and broker protections remain final safety authority.
- OpenRouter remains the model-provider path.
- Cost-optimized model routing and the accepted decision-chain audit remain in force.

## Current authorization — finish-line paper rollout

The operator has explicitly authorized one coordinated, outcome-driven tranche to reach the finished paper-trading operating state described in `docs/WORK.md`.

Claude may use a lead/orchestrator subagent to delegate work, verify stage gates and advance automatically through routine stages. **Do not return to the operator for micro-approval after every successful stage.** Stop only for operator-only privilege/credential actions, a material architecture/safety conflict, an unresolved product/value fork, or another boundary explicitly listed in `docs/WORK.md`.

The authorized end state includes:

- deployment of the accepted PR #48 fixes;
- production verification of SGOV funding and Tech batch completeness;
- staged enablement of the already-accepted intraday opportunity scanner on the existing cadence after its prerequisite gates pass;
- end-to-end health/observability verification across the existing paper-trading system;
- clean final governance/state closeout.

## Not authorized

- Live-broker trading.
- Direct stock shorting, options/theta strategies, or margin.
- New timers, daemons, services, databases, proxies, credential systems or other durable infrastructure outside accepted architecture.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Telegram command/control.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing/security/credential platform without an accepted architectural decision.
- Forcing/manufacturing a paper trade merely to prove the rollout.

## Handoff

Execute the stage-gated finish-line paper rollout in `docs/WORK.md`. The correct completion condition is operational readiness and verified wiring on the accepted architecture, not guaranteed trade generation.