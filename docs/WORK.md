# QAMC Current Work

Status: **FRICTIONLESS DELIVERY TOOLING AUTHORIZED | DEPLOY MERGED IEX CHART FIX | CORE RECOVERY IN NATURAL PAPER VALIDATION**

## Current integration truth

- GitHub `main` now includes PR #68 (`CLAUDE.md` minimal-sufficient execution discipline) and PR #69 (explicit Alpaca IEX feed for intraday chart bars plus the approved private Vite hostname fix).
- The PR #69 IEX diagnosis has been directly proven against production credentials: default/unset recent intraday stock data is rejected as SIP-unentitled; explicit IEX returns real SPY/AAPL bars; the branch `AlpacaBroker.get_intraday_chart_bars` path also returned real 15m bars.
- Actual VPS production remains on the older accepted runtime checkout at `2b3faaf69c0b842a08f991a9ca517a3989bdaf93` until the merged fix is deployed.
- Production has one intended tracked local delta: `config/settings.yaml` with `intraday_scan.enabled: true`.
- `ubuntu` / `qamc` / `dev` isolation remains hard. OneCLI remains the credential-delivery layer. Alpaca Paper remains the only authorized execution environment.
- The current chart spinner/empty intraday behavior is expected until production is updated from current accepted `main`.

## Product / architecture principle

QAMC is one autonomous Alpaca trading system. Alpaca Paper is the currently authorized execution environment, not a separate trading architecture.

Trading-critical path remains:

**discovery → Specialists → Portfolio Manager → AI Risk Manager → deterministic gate → funding → broker execution → position/exit management → reflection**.

Mission Control, Journal, search and Telegram are observational/read-side surfaces and must not become authoritative trading state or broker-write control paths.

## Next authorized work

### 0. Build the standing frictionless delivery path — IMPLEMENTATION AUTHORIZED

The operator explicitly authorizes one repo-owned, tested production deployment/verification entrypoint for routine accepted frontend and backend changes.

Desired standing workflow:

**`dev` implementation → private Vite/read-only verification when relevant → tests → push → ChatGPT/operator review and merge → one standardized `ubuntu` production deploy/verify → browser/runtime confirmation.**

The deployment/verification entrypoint must:

- be invoked as `ubuntu` and operate on the existing `qamc` production checkout through the existing account boundaries;
- deploy the exact current accepted GitHub `main` without inventing another release architecture;
- preserve the governed production `config/settings.yaml` override (`intraday_scan.enabled: true`);
- preserve OneCLI secret handling and never print credentials;
- refuse unexpected/unsafe production state instead of improvising around it;
- restart only what the existing deployment actually requires;
- verify deployed SHA, required service state, `/health`, and task-relevant read-only acceptance checks;
- fail closed with a concise blocker when safe deployment or verification cannot be proven;
- add no daemon, persistent service, proxy, database, framework, credential system, orchestration platform, or other new infrastructure.

Implementation guidance:

- reuse the repository's existing deployment/commissioning/systemd patterns where useful; do not perform another broad commissioning audit;
- inspect only what is needed to implement and test the entrypoint safely;
- this is workflow/tooling work, not authorization to investigate unrelated backend trading logic;
- use the `CLAUDE.md` minimal-sufficient execution rule: once prerequisites are known, implement and run the shortest decisive acceptance path;
- target 45–90 minutes; if the work is becoming a >2-hour effort or requires a material architecture change, stop and report the specific blocker rather than expanding scope.

### 1. Use the new path immediately to deploy and verify the merged chart fix

After the standing deployment entrypoint is ready, use it as the first real acceptance test:

- deploy current accepted `main` to the existing `qamc` production checkout;
- preserve `intraday_scan.enabled: true`;
- verify production remains Alpaca Paper and OneCLI/private-network boundaries remain intact;
- verify SPY and AAPL intraday `5m`, `15m`, and `1h` data return non-empty results through the production path;
- verify the actual Mission Control chart exposes and renders `5m Today`, `15m`, `1h`, and `1D` without the prior endless spinner/empty-bar failure;
- verify only the required production service(s) were restarted.

No additional external merge gate is required for PR #69: it has already passed ChatGPT/operator review and is merged into `main`. Do not re-litigate or re-review that merge before deployment.

### 2. Continue natural trading validation after deployment

Observe normal Alpaca Paper sessions without manufacturing trades. Natural sessions still need to demonstrate:

**opportunity discovered → evaluated → defensible bullish/bearish/neutral decision → executed when eligible → managed/exited → measured**.

Success is not "more trades." When QAMC does not trade, the reason must be specific and defensible.

## Standing fast-lane rule for future bounded fixes

For an already-authorized, bounded frontend/backend defect that stays within accepted architecture and does not alter trading/risk semantics:

1. `dev` diagnoses, implements and runs the shortest sufficient tests/preview.
2. Claude pushes a dedicated branch and stops at the external GitHub gate.
3. ChatGPT/operator reviews and merges.
4. `ubuntu` runs the standardized deploy/verify entrypoint once.
5. Stop when production verification passes.

Do not make the operator shuttle production diagnostics or credentials between `dev` and `qamc`. If production privilege or credentials are required, perform that proof in the standardized `ubuntu` step. Do not repeat architecture discovery, commissioning, package archaeology or unrelated verification after a decisive result exists.

## Flagged separately — not bundled into delivery-tooling work

`src/execution/broker.py::get_latest_price` builds latest-trade/latest-quote requests without an explicit Alpaca feed and silently degrades to `None` on failure. Because that path is trading-critical, it requires a separate authorized production investigation before any code change. Do not bundle it into the chart/deployment workflow work.

Lower-priority known issues remain news-narrative factual drift and `actual_provider` attribution oddity; do not interrupt the current delivery/deployment task for them.

## Hard boundaries

- **Current execution authorization is Alpaca Paper only.**
- No margin, options or direct stock shorting; bearish expression remains through approved inverse ETFs.
- Deterministic Python/broker protections remain final safety authority.
- Do not force/manufacture trades or weaken safeguards to increase activity.
- Do not create paper-only trading semantics.
- No new daemon/service/database/proxy/security/credential/orchestration architecture without separate explicit approval.
- Preserve `dev` / `qamc` / `ubuntu` isolation and OneCLI secret handling.
- Mission Control remains private/read-only; Telegram remains output-only.
- No public exposure of QAMC or OneCLI.
