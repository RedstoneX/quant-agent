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

Production is intentionally **not** at current `main`.

- Prior production baseline: `766877109b60026c94c00b38dbfb0e0c9630d236` (`7668771`, Mission Control cutover / PR #46 lineage).
- Current production: `9c736c158fec84129765c25a9429254d3602ad6b` (`9c736c1`).
- `9c736c1` is exactly one commit above `7668771` and contains only the reviewed Telegram restoration/security diff.
- Current GitHub `main`: `7f065c50287d8d55d191b89ada3f29abeb75b1e9`, which includes both PR #48 and PR #51.
- **Do not make production follow `main` or run `git checkout main` until the separately deferred PR #48 production rollout is explicitly authorized.**

## Telegram notification restoration — complete

The bounded Telegram restoration tranche is closed.

Repository work:

- PR #51 merged to `main` as `7f065c50287d8d55d191b89ada3f29abeb75b1e9`.
- The production-specific hotfix branch was built from `7668771` and deployed as exact commit `9c736c1`, avoiding PR #48.
- `src/notifier.py` now redacts the bot token from request-exception logging before any real Telegram credential is active.
- `scripts/telegram_test.py` provides the accepted non-trading delivery probe.

Runtime activation completed successfully on 2026-08-19:

- OneCLI running version verified as **1.45.0**.
- Real `TELEGRAM_BOT_TOKEN` is stored **only in OneCLI** as a generic secret scoped to `api.telegram.org`.
- OneCLI URL-path injection uses `pathTemplate: /bot{value}`.
- The secret is granted only to the existing QAMC OneCLI agent.
- `qamc/.env` contains only a harmless non-empty `TELEGRAM_BOT_TOKEN` placeholder plus ordinary `TELEGRAM_CHAT_ID`; the real bot token is not stored there.
- Existing `HTTPS_PROXY`, `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` OneCLI wiring remains intact.
- Preflight `getMe` returned HTTP 200 **before** production deployment, proving path substitution, proxy/CA wiring and Telegram egress.
- Exactly one non-trading `telegram_test.py` message returned `DELIVERED`.
- `run_if_et_window.sh` and `run_daily_export.sh` were verified to source the same runtime `.env` path used by the successful probe.
- No service/timer was started or restarted during activation; no broker order or trading action occurred.
- The operator rotated the initially exposed Telegram token before final activation.

Telegram remains notification/status output only. Commands, callbacks, webhooks, approvals and broker-write controls are not authorized.

## PR #48 — merged to main, production rollout still pending

PR #48 (`fix/sgov-liquidity-intraday-batch`) is merged to `main` as `70099c15097b77e6194a4cae247a9bacbea9a201` but is **not deployed to production**.

It contains three accepted fixes:

1. **SGOV funding semantics** — deployable cash uses owned raw cash plus convertible sweep value, while `CashSweeper.fund_buys()` reports only confirmed broker-cash increase after the sweep sale; execution's final raw-cash recheck remains authoritative.
2. **Intraday opportunity discovery** — bounded discovery on the existing `intra_check` cadence, including explicit current-session incomplete evidence and existing inverse ETFs for bearish expression.
3. **Tech batch-response completeness** — every submitted symbol reaches an explicit terminal outcome; missing batch results receive one bounded retry instead of silently disappearing.

The intraday scan ships **disabled** (`intraday_scan.enabled: false`). Deploying PR #48 and enabling the intraday scan remain separate decisions.

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

## Current authorization

There is **no active implementation tranche** after the Telegram closeout.

The next substantive production decision is the separately pending PR #48 rollout. It requires explicit authorization and must not be inferred from the fact that PR #48 is already merged to `main`.

Routine read-only review, evidence gathering, documentation maintenance and GitHub housekeeping remain permitted within existing governance.

## Not authorized without a new contract

- Live-broker trading.
- Direct stock shorting, options/theta strategies, or enabling margin.
- Enabling `intraday_scan` without explicit rollout authorization.
- Deploying current `main` to production merely for synchronization/cleanup.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing/security/credential platform without an accepted architectural decision.

## Handoff

Paper soak continues on production `9c736c1` with Telegram notifications active. PR #48 production deployment and any later `intraday_scan` enablement remain pending, separate work.