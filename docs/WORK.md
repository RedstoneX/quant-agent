# QAMC Current Work

Status: **COMMISSIONING — UPSTREAM ONECLI INTEGRATION + REAL PAPER-RUNTIME VERIFICATION**

## Goal

Bring the accepted QAMC system to a working, verified Alpaca Paper deployment on the OVH VPS using:

- the accepted OpenRouter routing for all 9 agents;
- the upstream-maintained OneCLI product as the credential-management/gateway layer if viable;
- the existing `ubuntu` / `qamc` / `dev` account separation;
- private services only;
- disabled trading timers until commissioning evidence justifies activation.

This is commissioning and integration work, not a redesign of the trading engine or Mission Control.

## Current environment model

### Runtime

- Account: `qamc`
- Purpose: QAMC runtime only
- Location: `/home/qamc/quant-agent`
- Mission Control/API deployed under supervised user systemd, bound privately.
- Trading timers installed but intentionally disabled.

### Development

- Account: `dev`
- Purpose: Claude Code and engineering work.
- Workspace: `/home/dev/projects/quant-agent`
- `dev` must not be given QAMC production/paper-runtime secrets merely for convenience.

### Administration

- Account: `ubuntu`
- Purpose: privileged VPS administration/recovery when `sudo` or host-level provisioning is genuinely required.

## Accepted routing state

All 9 QAMC agents are to route through OpenRouter using explicit provider `openrouter` and model `openai/gpt-5.5`. This is a routing-layer change only; no model diversification is authorized in this tranche.

Required real credentials for commissioning are expected to include:

- `OPENROUTER_API_KEY`
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `FRED_API_KEY`

Real values must never be pasted into ChatGPT, Claude, Git, screenshots, logs, or committed evidence.

## Credential architecture decision

Use the **actual upstream OneCLI product** rather than recreating its behavior.

OneCLI currently uses a transparent HTTPS gateway, scoped agent access tokens, an encrypted credential store, a web dashboard, and a Docker-based self-hosted stack. Its Docker/PostgreSQL requirements are not by themselves a reason to replace it with custom infrastructure; privileged installation belongs to the `ubuntu` administration boundary when required.

Commit `2207b0b74287101ea65ce79782081e51a27420ba` implemented a home-grown credential proxy after encountering that installation requirement. That implementation is explicitly rejected architecture. Do not deploy, extend, harden, or revive it. Empirical compatibility findings from the work may be reused as test evidence only.

If upstream OneCLI proves materially incompatible with QAMC, the VPS, or the accepted isolation model after a serious engineering attempt, **STOP AND ASK**. A blocker is not permission to build a replacement gateway, vault, proxy, daemon, or security layer.

## Authorized engineering scope

Claude should own the engineering loop from the `dev` environment and continue until a genuine operator-only boundary exists. It may:

- rehydrate from the current `main` branch and relevant accepted architecture contracts;
- inspect current upstream OneCLI documentation/source and the installed VPS capabilities rather than relying on stale assumptions;
- determine the smallest safe upstream OneCLI deployment topology on this single VPS;
- preserve private binding and `dev` / `qamc` isolation;
- prepare reproducible host/runtime configuration needed for upstream OneCLI;
- use the empirical `httpx` / `requests` / `urllib` findings from the rejected proxy work to design compatibility tests for OpenRouter, Alpaca, and FRED;
- verify QAMC uses placeholders/non-secret runtime configuration correctly through OneCLI;
- run applicable automated tests and targeted runtime checks;
- update only the existing governance/state files when the checkpoint materially changes;
- commit and push a dedicated Claude branch for independent review.

Claude should use outcome-driven execution, verification loops, context hygiene, subagents/worktrees/background tools only when useful, and autonomous routine engineering decisions as governed by `CLAUDE.md`.

## Operator-only boundaries

Claude should stop only when one of these is genuinely required:

1. privileged host action that `dev` cannot perform safely (for example Docker/host package provisioning under `ubuntu`);
2. entry of real secrets directly into the approved credential system;
3. a material architecture/security incompatibility requiring a choice outside the accepted OneCLI direction.

Bundle operator actions into one concise intervention where safe. Do not ask the operator to perform GitHub administration or routine engineering work that Claude/ChatGPT can perform directly.

## Verification before commissioning checkpoint

Before asking to enable trading timers, establish objective evidence that:

- upstream OneCLI is actually running and remains private;
- `dev` cannot read the real QAMC credentials;
- `qamc` can make the required OpenRouter, Alpaca Paper, and FRED calls through the approved credential path without holding exposed real keys in normal project files;
- all 9 agents still resolve to OpenRouter / `openai/gpt-5.5`;
- Alpaca endpoint is Paper only;
- Mission Control remains read-only and non-critical;
- no secret material entered Git, logs, screenshots, shell history artifacts committed to the repo, or client surfaces;
- the full applicable test suite remains clean;
- failure of OneCLI or Mission Control fails safely and does not create a path to unauthorized live trading.

Trading timers remain disabled until this commissioning evidence is reviewed and activation is appropriate.

## Checkpoint status — 2026-08-12 (OneCLI host-provisioning requirements determined — stopped for `ubuntu` action)

Rehydrated from current `main` (PR #26 merged), not from the old deployment branch. Preserved the empirical `httpx`/`requests`/`urllib` proxy-compatibility findings from the rejected `2207b0b` work in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` (trimmed to evidence only — the rejection rationale now lives in this file and `STATE.md`, not duplicated).

Investigated upstream OneCLI's actual current requirements directly against its source, not from stale notes:
- Install script (`onecli.sh/install`) unconditionally requires Docker + Compose + a running daemon.
- The upstream `docker/docker-compose.yml` itself: two services (`postgres:18-alpine`, `ghcr.io/onecli/onecli:latest`), both bind `127.0.0.1` only by default (`ONECLI_BIND_HOST`), no `.env` file strictly required (all vars have working defaults), dashboard on `10254`, gateway on `10255`.
- `dev` (verified live, this account): no `docker`, no passwordless `sudo`. Per the accepted account-isolation model, `qamc`/`dev` should not be added to the `docker` group either — that grants root-equivalent host access, which would defeat the isolation those accounts exist to provide. So the entire Docker install *and* bringing up OneCLI's stack needs to run as `ubuntu`, not just the Docker install step.

Wrote the exact, minimal command set for that as `ops/onecli/README.md` (a runbook, not custom code — every command either installs Docker via its own official apt repository or runs the upstream OneCLI repo's own `docker-compose.yml` unmodified).

**Stopping here for the bundled `ubuntu` action** (operator-only boundary #1 — privileged host provisioning): install Docker Engine + Compose plugin, then bring up OneCLI's own compose stack. Exact commands in `ops/onecli/README.md`. No real trading/LLM secrets are needed for this step. Once confirmed running and private, the next `dev`-side work is creating a QAMC agent/gateway token in OneCLI and wiring routes for OpenRouter/Alpaca/FRED (still no real secret values needed for that) — entering the four real values remains a later, separate, operator-only step.

Zero `src/` changes this slice. Trading timers untouched (still disabled). All 9 agents still OpenRouter/`openai/gpt-5.5` per `config/settings.yaml` on `main` — not touched.

## Checkpoint status — 2026-08-12, later (VPS security hardening applied and verified — OneCLI commissioning resumes)

A side branch (`claude/vps-security-hardening-t8m3qz`) produced `ops/security/vps-hardening-plan.md` and an idempotent `ops/security/harden.sh` from a `ubuntu`-produced host audit, reviewed and approved, dry-run reviewed, then applied by `ubuntu`. Verified live from `dev` afterward, not just taken on report:

- `uptime` showed ~2 minutes and `uname -r` showed `6.8.0-137-generic` (the previously-staged kernel) — the pending reboot completed and the new kernel is running.
- `fail2ban` active, started at boot time.
- Tailscale connected, same tailnet as before; no subnet router or exit node configured (unchanged from the audit — never in scope for this pass).
- `/var/run/reboot-required` no longer present.
- UFW's own active/deny-incoming state and the `btop`/`iftop` install weren't independently checkable from `dev` without root, but were confirmed by the operator and are consistent with everything that was checkable.

This pass never touched `sudo` membership, Docker, OneCLI, or anything under `config/`, `src/`, or QAMC's runtime — confirmed via `git diff --stat` on the hardening branch before it was pushed. OneCLI commissioning resumes from exactly where it stopped: waiting on the bundled `ubuntu` action in `ops/onecli/README.md` (Docker install + bringing up OneCLI's own compose stack). Nothing about that step changed.

## Checkpoint status — 2026-08-12, later still (OneCLI live and privately bound — verified, not executed)

`ubuntu` completed the bundled action from `ops/onecli/README.md`. Verified from `dev` with read-only, non-privileged checks only (no system changes made this pass):

- Docker installed and running (`docker --version` → 29.7.2).
- Process listing (no Docker socket access needed) shows two `containerd-shim` instances, a `postgres` process tree, and `onecli-gateway --port 10255 --data-dir /app/data` — matching the upstream compose file's two services exactly.
- `curl 127.0.0.1:10254` → `200` (dashboard up). `curl 127.0.0.1:10255` → `400` (expected: it's a CONNECT-based forward proxy, not a web server — a plain GET correctly gets rejected).
- Both ports bound `127.0.0.1` only (`ss -tln`) — not `0.0.0.0`, not the public IP.
- Isolation confirmed, not just assumed: `dev` is **not** in the `docker` group (`docker ps` → `permission denied while trying to connect to the docker API`), and still cannot read `/home/qamc`. `dev`'s own shell environment has no stray `HTTPS_PROXY`/credential values. `config/settings.yaml` on this branch is unchanged (still OpenRouter/`openai/gpt-5.5` throughout).
- Firewall (`ufw status`) isn't independently checkable from `dev` without root — relying on the prior hardening checkpoint's verification plus this pass's confirmation that nothing about that changed.

No real credentials exist anywhere in this chain yet. Recommended next step (not yet executed): create a QAMC agent/gateway token in OneCLI's dashboard/API and configure routes for `openrouter.ai`, `paper-api.alpaca.markets`, `data.alpaca.markets`, `api.stlouisfed.org` — all still no real secret values required, since routes can be scaffolded with placeholder/pending credential slots the same way the reverted custom-proxy work validated the HTTP-stack compatibility in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`. Entering the four real values into OneCLI's vault remains a separate, later, operator-only step.

## Checkpoint status — 2026-08-12, later still (OpenRouter integration point determined and proven — zero code changes)

Operator created Custom Secret "OpenRouter - QAMC" (host `openrouter.ai`) in OneCLI's dashboard and assigned it to the Default Agent — real key entry happened only there, never through `dev` or chat. Determined and proved the integration mechanism, not assumed:

- Queried the live instance's own `GET /api/container-config` (documented API, `dev` reachable since it's on `127.0.0.1`) to get the exact env vars OneCLI expects a consuming process to set, rather than guessing from product marketing. One correction applied: it returns `host.docker.internal` as the proxy host, which only resolves inside a Docker container — QAMC's trading engine/Mission Control are bare `qamc` processes, so `127.0.0.1` is the correct host (confirmed the gateway is also bound there directly).
- **Empirically proved** credential injection rather than trusting the mechanism description: sent an obviously-fake `Authorization: Bearer` value to `openrouter.ai/api/v1/auth/key` (an endpoint that validates the key) both directly (`401`) and through the OneCLI gateway with CA trust (`200`) — the only variable changed was the routing path, proving the gateway substitutes the real key server-side. Response bodies were discarded (`-o /dev/null`) both times; the real key and any account metadata were never read.
- Full detail and the corrected env-var values in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.

## Checkpoint status — 2026-08-12, later still (Alpaca + FRED integration verified — two real gaps found, not fixed here)

Operator created the remaining three Custom Secrets (FRED, Alpaca Key ID, Alpaca Secret) in OneCLI's dashboard — real values entered only there, `dev` never saw them. Confirmed via code inspection that `AlpacaBroker` (`src/execution/broker.py`) and `MacroDataProvider` (`src/data/macro.py`) both construct their SDK clients (`TradingClient`/`StockHistoricalDataClient`/`Fred`) with no custom session/opener, so the same zero-code-change conclusion as OpenRouter applies — confirmed via `GET /api/secrets` (metadata only) that all four secrets' injection method (header/header/header/query-param) matches exactly what each library needs.

**Two real gaps found empirically, not fixed — both are edits to real-credential routing on the operator's own OneCLI dashboard, not something `dev` did unilaterally, same boundary as the original secret creation:**

1. Only `OpenRouter - QAMC` is granted to the Default Agent (`GET /api/agents` shows `secretMode: "selective"` — grants are a separate step from creating a secret). Fake-header requests through the gateway to Alpaca/FRED came back exactly as unauthenticated as calling the real APIs direct with no gateway at all (`401`/`400`), proving no injection happened for those three yet.
2. Both Alpaca secrets are scoped to `paper-api.alpaca.markets` only; QAMC's `StockHistoricalDataClient` also calls `data.alpaca.markets` (confirmed via the same fake-header gateway test against that host: still `401`). OneCLI's `hostPattern` supports a leading-subdomain wildcard, so widening the two existing secrets to `*.alpaca.markets` (not new secrets) would cover both.

Every verification request's response body was discarded (`-o /dev/null`); only HTTP status codes were compared, matching the same discipline as the OpenRouter proof. Full detail in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.

`.env.example` updated with the one additional env var Alpaca needs beyond OpenRouter's (`REQUESTS_CA_BUNDLE` — `requests` doesn't honor `SSL_CERT_FILE`); FRED needs nothing beyond what OpenRouter already requires.

**Zero `src/` or `config/` changes required.** `src/agents/base.py`'s OpenRouter branch already constructs its OpenAI SDK client without a custom `http_client`, so it already inherits `httpx`'s default environment-driven proxy/CA behavior — the exact mechanism OneCLI needs. `_OPENROUTER_BASE_URL` and `config/settings.yaml`'s provider/model fields are unrelated to credential delivery and stay untouched. The only change made: a documentation-only pointer added to `.env.example` (placeholder values only, no real token) explaining the two env vars (`HTTPS_PROXY`, `SSL_CERT_FILE`) whoever has `qamc` access needs to add to `/home/qamc/quant-agent/.env` — `dev` cannot apply that directly (`dev` cannot write into `/home/qamc`), and the live agent token/CA cert should be fetched fresh by whoever applies it (`curl http://127.0.0.1:10254/api/container-config` from `qamc` or `ubuntu`) rather than relayed through chat.

Trading timers, Alpaca, and FRED routes are unchanged — out of scope for this pass per the operator's explicit "do not create another credential entry."

## Hard boundaries

- Alpaca **Paper only**.
- No deterministic trading/risk semantic redesign.
- No broker-write Mission Control operations.
- No public exposure of QAMC or OneCLI.
- Do not collapse account/trust boundaries.
- No custom credential proxy/vault replacement.
- No secrets in Git/chat/client evidence.
- No dedicated dashboard visualization/UX polish.
- Claude does not merge its own PR, force-push, or push implementation directly to `main`.
