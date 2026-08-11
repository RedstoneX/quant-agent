# QAMC Current Work

Status: **VPS DEPLOYMENT / HARDENING — INFRA DEPLOYED, PARTIAL VERIFICATION, BLOCKED ON SECRETS + ROOT — STOPPED FOR OPERATOR INPUT**

## Goal

Move the accepted QAMC paper-trading engine + read-only Mission Control bundle from cloud/ephemeral staging into the intended small Linux VPS runtime, harden the operational deployment, verify the deployed system there, and produce a bounded branch/checkpoint for independent review.

This is a deployment tranche, not a redesign. Preserve the accepted trading engine, read-only Mission Control boundary and Stage 4–5 behavior unless a real deployment defect requires a narrowly-scoped fix.

## Deployment target

- Provider: OVH VPS.
- Hostname: `vps-37b5f875.vps.ovh.us`.
- IPv4: `135.148.120.105`.
- OS: Ubuntu 24.04.
- Storage: 100 GB; no additional storage purchased.
- Automated daily OVH backup: active.
- Current plan: $14.50/month, no commitment.
- Operator is currently working entirely from an iPad; do not require a desktop/laptop merely to bootstrap or operate this tranche.

## SSH bootstrap prerequisite (superseded — see STATE.md operational correction)

The cloud-bootstrap plan below did not happen: Anthropic's cloud environment could not open outbound TCP/22. Claude Code instead runs through a Mac-hosted SSH connection straight to the OVH VPS as `qamc`, using a persistent Ed25519 key the operator installed directly. On connecting, `authorized_keys` was checked and contained exactly one key (`qamc-vps-deploy-20260809`) — no disposable bootstrap credential was present to revoke. `qamc` is in the `sudo` group but has no working non-interactive sudo in this session (no cached auth, no NOPASSWD rule) — see the "Blocked" section below for what that constrains.

Original plan, kept for reference only:
1. ~~Generate a disposable Ed25519 keypair in the Claude cloud environment.~~
2. ~~Keep the private key only in that environment...~~
3. ~~Give the operator only the public key...~~
4. ~~After operator confirmation, verify SSH access...~~
5. Establish the persistent VPS access/runtime arrangement appropriate to the accepted architecture. — **done**, via the operator's own Mac-hosted key.
6. ~~Before checkpoint handoff, remove/revoke the disposable bootstrap credential...~~ — **moot**, none existed.

Do not expose or commit secrets.

## Authorized engineering scope

Claude owns repository-level deployment planning and may choose the simplest safe implementation details after inspecting the actual codebase. The outcome must establish, as appropriate to this repository:

- reproducible deployment of the accepted `quant-agent` engine and Mission Control API/UI bundle to the VPS;
- Python/runtime/system dependencies needed on Ubuntu 24.04;
- secrets/environment configuration outside Git and client surfaces;
- durable application/data paths and permissions;
- process supervision and restart-on-failure/reboot behavior for the trading engine and Mission Control as separate operational components where required by the accepted non-critical read-side boundary;
- private operator access rather than unnecessary public exposure;
- logs, health checks and basic operational recovery sufficient to distinguish trading-engine failure from Mission Control failure;
- backup/recovery handling consistent with the existing OVH daily backup and the application's persisted artifacts;
- evidence that Mission Control failure still has zero effect on deterministic trading/risk/broker protections.

Claude may add deployment scripts/configuration/documentation where genuinely useful. Avoid unnecessary infrastructure, distributed services, containerization or reverse-proxy complexity unless the repository/runtime evidence justifies them.

## Verification and acceptance evidence

Before handoff, Claude must:

- run the full applicable automated test suite in the deployed/runtime context;
- exercise the deployed engine/API/UI paths and health behavior on the VPS;
- browser/runtime verify the cockpit using the permanent frontend-verification rule, including representative desktop and iPad-sized scenarios and honest populated/empty/error/degraded states relevant to this deployment;
- verify restart/reboot recovery and persistent data behavior;
- verify Mission Control can fail/restart independently without weakening or stopping the trading engine's deterministic protections;
- verify no secret material entered Git or committed evidence;
- remove/revoke the disposable SSH bootstrap credential when persistent access is established;
- update `STATE.md`, `WORK.md` and the human `PROJECT_COMPASS.md` only as needed for the checkpoint;
- commit and push the bounded deployment branch, then **STOP** with a concise checkpoint report for ChatGPT/operator review.

Operator UAT happens only after fresh independent review of the pushed result. Claude must not declare the deployed MVP accepted on its own.

## Checkpoint status — 2026-08-10

**Done and verified** on branch `claude/vps-deployment-hardening-q3f7k2`:
- venv + deps installed on the VPS without root (`venv --without-pip` + `get-pip.py`, since `python3.12-venv` isn't installed and there's no sudo path to install it).
- Full test suite in the deployed venv: 1558 passed, 0 failed.
- `quant-agent-api.service` (Mission Control API/UI, `127.0.0.1:8800`, `Restart=always`) installed, enabled, and running under `systemd --user` with `loginctl enable-linger qamc` set for logout/reboot persistence. Verified via HTTP: `/health` 200 with correct graceful `broker_reachable:false` degradation on placeholder keys, `/ui` 200, kill-9 crash-recovery within 5s.
- All six trading-mode timer/service pairs + the daily P&L export pair installed (`daemon-reload`'d) but **left disabled** — starting them against placeholder `.env` values would just burn real LLM/broker retry budget on guaranteed-401 calls with no verification value.
- Confirmed independence: restarting/crashing the API service does not touch the (disabled) trading timers — separate systemd units, no dependency edges.

**Blocked, needs operator action, not worked around:**
1. `.env` on the VPS is still the placeholder template (`chmod 600`). Real `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`FRED_API_KEY` (and optionally `TELEGRAM_*`, `HEALTHCHECKS_URL`) need to land on the VPS via `scp`/`sftp`/direct edit — not chat. Until then the trading timers stay disabled and "exercise the deployed engine" can't be meaningfully run.
2. Headless Chromium (installed via `pip install playwright` + `playwright install chromium`, no root needed for the download) fails to launch — missing shared libs (`libatk-1.0.so.0` etc.) that require `sudo apt-get install`. Full desktop/iPad screenshot verification per `.claude/rules/frontend-verification.md` is blocked on one interactive sudo command; HTTP-level runtime verification of `/health` and `/ui` was completed instead.

Neither blocker was inferred or bypassed. Reboot-persistence is inferred from `Linger=yes` + unit `enable` (a symlink under `default.target.wants`), not from an actual reboot — a real reboot wasn't taken without asking, since it wasn't necessary to establish that inference and rebooting someone else's live VPS isn't a call to make silently.

## Checkpoint status — 2026-08-11 (OpenRouter routing + credential architecture)

This slice ran from the `dev` account (Claude Code's own development environment on the same VPS, distinct from `qamc`), not from `qamc` directly — `dev` cannot read `/home/qamc`, cannot query `qamc`'s `systemd --user` instance, and never held a real secret at any point in this slice.

**Done and verified:**
- All 9 agents (`tech_analyst` … `meta_reflector`) now route through OpenRouter (`<agent>_provider: "openrouter"`, `<agent>_model: "openai/gpt-5.5"`) — commit `5c172b583e1be83afa2cee99763d16e277413679`, pushed. Verified via `resolve_provider()` against the live config: 9/9 resolve to `openrouter`. This is config-only; no code changes were needed — `_OPENROUTER_BASE_URL` is already a real provider seam (`docs/architecture/MODEL_PROVIDER_ARCHITECTURE.md`).
- OneCLI (the open-source AI-agent credential gateway suggested for the credential-management layer) was investigated by fetching its actual install script and source, not summarized secondhand: it unconditionally requires Docker + Docker Compose + PostgreSQL with no non-Docker local mode. `dev` has neither Docker nor passwordless sudo. Installing the product itself was not possible from this account without operator-provisioned infrastructure.
- Rather than stop there, OneCLI's underlying architectural *pattern* — an HTTPS forward proxy that intercepts outbound calls via `HTTPS_PROXY`, TLS-terminates with a locally-trusted CA, and rewrites a placeholder credential to the real one before forwarding — was extracted and empirically validated against QAMC's actual dependency code (not assumed): `httpx` (OpenRouter/`openai` SDK transport) honors `HTTPS_PROXY`+`SSL_CERT_FILE`; `requests` (`alpaca-py`'s actual transport) honors `HTTPS_PROXY` but needs `REQUESTS_CA_BUNDLE` specifically — `SSL_CERT_FILE` alone does *not* work for it, a real gap this testing caught before it could surprise a real deployment; `urllib` (`fredapi`'s actual transport, not `requests`) honors `HTTPS_PROXY`/`https_proxy` and `SSL_CERT_FILE`, with query-parameter injection (FRED's `?api_key=`) proven end to end. Two-header injection (Alpaca's `APCA-API-KEY-ID`+`APCA-API-SECRET-KEY`) was proven mechanically sound.
- Given that evidence, the engineering call was made to reject installing the OneCLI product but implement its pattern directly: a new ~300-line stdlib-only proxy (`ops/credential-proxy/gateway.py`), since a multi-tenant Docker/Postgres/dashboard stack is disproportionate for "one process, four known credentials, three known upstream hosts, one operator." See `docs/architecture/CREDENTIAL_PROXY.md` for the full rationale and evidence.
- Full deliverable: `ops/credential-proxy/` (proxy + systemd unit with least-privilege sandboxing + operator setup script + runbook), `docs/architecture/CREDENTIAL_PROXY.md`, a 4-line pointer comment in `.env.example`. Zero `src/` files touched — verified via `git status`/`git diff` before commit, not assumed.
- Full test suite re-run in a freshly-built dev-side venv (`venv --without-pip` + `get-pip.py`, same no-root pattern as the original deployment): **1558 passed, 0 failed** — matches the accepted baseline exactly, confirming zero regressions from this slice.
- Independent review of this diff (fresh agent instance, not the author, adversarial brief) found 2 BLOCKERs before push: a request-body relay bug that could hang on ordinary POST traffic (Alpaca orders, OpenRouter completions) depending on TCP write timing, and no caller authentication on the proxy (any local process, including `dev`, could use it exactly as if it held the real credentials). Both fixed — body relay redesigned to a framing-agnostic bidirectional pump instead of manual `Content-Length` counting, and a required shared-secret `Proxy-Authorization` check added — and both fixes empirically re-verified (reproduced the exact hang scenario, confirmed it no longer hangs and body content arrives intact; confirmed 407 for missing/wrong token and success for the correct one across all three HTTP libraries). A third finding (IMPORTANT) was that the original qamc integration only patched Mission Control's systemd unit and never reached the trading engine's separate `.env`-sourcing path — fixed by moving the integration point to `.env` itself. Full detail in `docs/architecture/CREDENTIAL_PROXY.md`'s "Independent review" section.

**Blocked, needs operator action, not worked around** (bundled into one consolidated set — see `ops/credential-proxy/README.md`):
1. Provisioning the dedicated `credproxy` system account, its CA, and the systemd service (`ops/credential-proxy/setup.sh` — needs `sudo`, `dev` has none).
2. Entering the four real credential values directly into `credproxy`'s `routes.json` — operator only, never through chat, never through `dev`.
3. Adding three lines (`HTTPS_PROXY` with the generated auth token embedded, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`) to `/home/qamc/quant-agent/.env` — the project's existing single source of truth for secrets, covering both `quant-agent-api.service` and the (still-disabled) trading engine's `.env`-sourcing path — then restarting `quant-agent-api.service`. `dev` cannot write into `/home/qamc`, so this needs whoever has `qamc` access.

None of the four real credentials exist anywhere `dev` can reach at any point in this slice, before or after.

## Hard boundaries

- Alpaca **Paper only**.
- No deterministic trading/risk semantic redesign.
- No broker-write Mission Control operations.
- No secrets or fake production trading state in client/UI surfaces or Git.
- Mission Control remains read-only and non-critical to trading.
- Do not start dedicated dashboard visualization/UX polish.
- Do not start later learning/write-control work.
- Claude does not merge PRs, force-push, or push implementation directly to `main`.

## Engineering authority and escalation

Within this contract Claude should act as engineering lead: inspect the repository, select implementation architecture, choose subagents/workers and maximize safe parallelism without asking the operator to make routine technical decisions.

Escalate only for a genuine operator product/value trade-off, a material conflict with accepted architecture/safety/scope, or an external-access decision that cannot be safely inferred. Otherwise implement, verify, integrate, push and stop at the checkpoint.
