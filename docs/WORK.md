# QAMC Current Work

Status: **UBUNTU ENGINEERING AUTONOMY | QAMC RUNTIME ISOLATION | OPERATOR PROMOTION CONTROL | NATURAL PAPER VALIDATION**

## Current integration truth

- Production has been reported and verified at `a6758f935910c5cf380cc6a7acedc5f3b78f6366` after PR #69 deployment.
- PR #69 fixed the intraday chart data path by explicitly requesting Alpaca IEX for 5m/15m/1h bars. Production verification reported non-empty SPY/AAPL bars and working `5m Today`, `15m`, `1h`, and `1D` chart controls.
- The chart live-price/current-price truth issue was already fixed by commit `796558f184f8dd800c7e1cbb57f11173ad3d6f6b`. It is not an outstanding task.
- The previously flagged `get_latest_price` missing-feed concern is not an established defect: Alpaca latest trade/quote requests default to the best feed available to the subscription; current probes show IEX succeeds and explicitly requested SIP is rejected as unsubscribed, as expected. The method's `None` result on an actual API exception is intentional and tested fail-closed behavior.
- Production remains Alpaca Paper. The seven existing timers remained intact, Mission Control remained private/read-only, and `config/settings.yaml: intraday_scan.enabled: true` was preserved.
- GitHub `main` may move ahead with documentation or later accepted work. **Production does not automatically follow `main`.**

## Stabilization account model — HARD RULE

Use two active accounts until QAMC is stable:

### `ubuntu` — engineering/operator

Use `ubuntu` for Claude/Codex sessions, engineering checkout/worktrees outside `/home/qamc`, Git/GitHub, development tooling, targeted tests/builds, private Tailscale Vite/browser work, Docker/sudo engineering tasks, and approved deployment orchestration.

### `qamc` — runtime only

`qamc` owns `/home/qamc/quant-agent`, runtime `.env`/OneCLI wiring, user services/timers, and QAMC Paper execution. Do not run Claude/Codex as `qamc` or turn it into a general engineering account.

### `dev` — parked

Do not use `dev` in the normal workflow or expand its permissions during stabilization.

## Standing delivery workflow — HARD RULE

Engineering inside an already-authorized task is autonomous under `ubuntu`: diagnose, implement, test, preview, browser-verify, commit and push without repeated operator prompts.

Implementation promotion remains reviewable. Claude does not independently merge its own implementation work or mutate the `qamc` runtime unless that promotion is explicitly authorized. Once production deployment is authorized, `ubuntu` performs the shortest safe privileged deploy/verify operation directly; do not bounce the operator among accounts.

## Friction-reduction rules

1. No normal use of `dev` and no manual account ping-pong.
2. Private DEV preview/browser verification is standing-authorized for relevant engineering work.
3. For bounded fixes, read only current authority plus relevant code and run the shortest decisive verification.
4. Targeted tests first; no default full-suite, commissioning rerun, multi-agent fan-out or broad repository archaeology.
5. Stop when the requested result is proven; repeated re-validation without new evidence is not diligence.
6. Keep handoffs concise: changed / verified / unresolved blocker / exact promotion state.
7. Preserve the `ubuntu` engineering vs `qamc` runtime boundary; do not add new lockdown/security infrastructure during stabilization without a real need.
8. Do not infer current defects from historical notes. Reopen a resolved area only from current operator or production evidence.
9. After an authorized production change, bundle preflight/deploy/restart/acceptance into one safe intervention.

## Active finish line

### Natural Alpaca Paper validation

The engineering machinery is considered present; the remaining substantive acceptance is natural evidence that QAMC behaves coherently in ordinary markets:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

Success is not a target number of trades. Do not manufacture opportunities, force orders, weaken risk controls, or hindsight-tune the system to create evidence.

Use the existing Mission Control, journal and Telegram read-side evidence to determine:

- what opportunity was discovered;
- what the specialists, Portfolio Manager and AI Risk Manager concluded;
- what deterministic risk/funding/execution did;
- why an eligible trade did or did not execute;
- how any resulting position was managed/exited;
- what the measured result and missed-opportunity evidence show.

When QAMC does not trade, the reason should be specific and defensible rather than an unexplained absence of activity.

## Parked follow-ups — not blockers

- news-narrative factual drift;
- `actual_provider` attribution oddity.

Do not interrupt natural validation for these unless current evidence shows they materially distort decision quality, truthfulness, or operator understanding.

`get_latest_price` is **not** on this list solely because its request omits `feed`; that concern has been reconciled. Reopen only on concrete production evidence.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential/orchestration architecture without separate explicit approval.
- Keep `qamc` runtime-only and preserve OneCLI secret handling.
- Do not expand `dev` privileges during stabilization.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.

**No active engineering blocker is currently established.**
