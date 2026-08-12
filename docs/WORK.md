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
