# QAMC Current Work

Status: **ALPACA PAPER SOAK ACTIVE — TELEGRAM RESTORATION COMPLETE — PR #48 PRODUCTION DEPLOYMENT / INTRADAY ROLLOUT PENDING**

This file is the current work/handoff contract only. Historical implementation detail belongs in Git history and accepted architecture/verification records, not in an ever-growing work log.

## Closed tranche — Telegram notification restoration

The existing Telegram notification capability has been restored, secured and activated in production.

Accepted evidence:

- PR #51 merged to `main` as `7f065c50287d8d55d191b89ada3f29abeb75b1e9`.
- Production-specific hotfix `9c736c158fec84129765c25a9429254d3602ad6b` was built directly on the prior production baseline `766877109b60026c94c00b38dbfb0e0c9630d236` so PR #48 could not ride along.
- Production now runs exact `9c736c1`.
- Telegram notifier request failures redact the bot token before logging.
- OneCLI 1.45.0 provides the real bot token using URL-path injection for `api.telegram.org` with `pathTemplate: /bot{value}`.
- The real bot token is stored only in OneCLI; `qamc/.env` contains only a harmless placeholder plus ordinary `TELEGRAM_CHAT_ID`.
- `getMe` preflight returned HTTP 200 before deployment.
- Exactly one non-trading `scripts/telegram_test.py` message returned `DELIVERED`.
- Existing scheduled wrappers were verified to source the same runtime `.env` path.
- No trading action, broker order, service restart, PR #48 deployment or intraday enablement occurred during activation.

Telegram remains **notification/status output only**. No Telegram command/control path is authorized.

## Pending production work — PR #48

PR #48 (`fix/sgov-liquidity-intraday-batch`) is merged to `main` as `70099c15097b77e6194a4cae247a9bacbea9a201` but remains **undeployed**.

Its accepted scope is:

1. **SGOV funding correctness** — size against owned deployable liquidity; confirm actual broker cash increase after SGOV liquidation; keep execution's final raw-cash gate authoritative.
2. **Intraday opportunity discovery** — use the existing `intra_check` cadence, current-session incomplete evidence, cooldown/dedup and the existing Specialist → PM → AI Risk → deterministic gate → execution chain.
3. **Tech batch completeness** — no silent symbol loss; one bounded retry for missing batch outputs; explicit terminal outcomes.

Important rollout rule:

- Deploying PR #48 code and enabling `intraday_scan` are **separate decisions**.
- `intraday_scan.enabled` remains `false` until explicitly authorized.
- Do not synchronize production with `main` as a housekeeping action; production is intentionally pinned at `9c736c1` until the rollout decision is made.

## Current implementation authorization

**None.**

Do not start a new implementation tranche merely because PR #48 is pending. The operator/ChatGPT must explicitly authorize the next production action.

Routine activities that remain allowed:

- read-only production/repository inspection;
- paper-soak evidence review;
- documentation maintenance;
- GitHub review/integration/housekeeping;
- verification that does not alter trading or credential boundaries.

## Hard boundaries

- Alpaca Paper only; no live trading.
- No margin, options or direct stock shorting.
- Approved bearish expression remains through existing inverse ETFs.
- Deterministic Python/broker protections remain final.
- No broker-write Mission Control controls.
- Telegram remains output-only.
- No new daemon/service/database/proxy/security/credential architecture without explicit architectural approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Do not expose QAMC or OneCLI publicly.
- Do not deploy current `main` or enable `intraday_scan` without explicit authorization.

## Next decision checkpoint

When the operator is ready, review and authorize the PR #48 production rollout as one bounded outcome-driven tranche. A sensible rollout order is:

1. deploy the already-reviewed PR #48 code while keeping `intraday_scan.enabled: false`;
2. verify SGOV funding/batch fixes during the paper soak;
3. separately decide whether to enable the intraday scan and what evidence/observability is required for that enablement.

Until that decision, the correct state is: **paper soak active on production `9c736c1`, Telegram active, PR #48 undeployed, intraday scan disabled.**