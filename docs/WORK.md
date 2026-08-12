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

## Checkpoint status — 2026-08-12, later still (all four credentials verified working end-to-end through OneCLI)

Operator applied both fixes above. Re-verification found a **third** gap, again diagnosed from live metadata rather than guessed: both Alpaca secrets had `injectionConfig.valueFormat: "Bearer {value}"` — correct for OpenRouter's OAuth-style `Authorization` header, wrong for Alpaca's `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY`, which need the raw value with no prefix (FRED's param format, `"{value}"`, was the tell — it's correctly unprefixed). The gateway was sending `APCA-API-KEY-ID: Bearer <real-key>`, which Alpaca correctly rejects. Operator corrected `valueFormat` to plain `{value}` on both.

**Final verification — all four now succeed through the gateway with fake placeholder credentials, and fail the same way direct (no gateway) as they did throughout this whole investigation:**

| Target | Direct | Through gateway |
|---|---|---|
| OpenRouter | `401` | `200` |
| Alpaca trading (`paper-api.alpaca.markets`) | `401` | `200` |
| Alpaca data (`data.alpaca.markets`) | `401` | `200` |
| FRED | `400` | `200` |

`dev` never read, logged, or held any real credential value at any point across the three-round diagnosis — only status-code comparisons and non-value metadata (host/path patterns, injection field *names*, grant lists). All three fixes were applied by the operator directly in OneCLI; `dev` diagnosed each with a reproducible test and made none of the credential-routing edits itself. Full detail in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.

Trading timers, Alpaca, and FRED routes are unchanged — out of scope for this pass per the operator's explicit "do not create another credential entry."

## Checkpoint status — 2026-08-12, later still (runtime wiring is the one remaining bounded task)

Re-confirmed, not assumed: `dev` still cannot write into `/home/qamc` (`touch` → `permission denied`) and has no access to `qamc`'s `systemd --user` D-Bus session (`systemctl --user -M qamc@` → `permission denied`). Both are genuinely `dev`-cannot-perform-safely, not a preference. Captured a baseline from `dev` over loopback (no filesystem access needed — Mission Control's port is reachable regardless of which account owns the process): `/health` currently reports `broker_reachable: false`.

The exact minimal runtime change — three `.env` lines, no code, no new secrets, existing placeholders untouched — is now written as `ops/onecli/README.md` step 4, so it isn't duplicated here. Applying it and restarting `quant-agent-api.service` is the one remaining bounded task; `dev` cannot execute it. `broker_reachable` flipping to `true` on `/health` is the objective completion signal — `dev` can verify that independently once applied, without needing `/home/qamc` access.

Re-checked again this pass: unchanged (`broker_reachable` still `false`, `/home/qamc` still `permission denied`). Still waiting on the same operator action — no new blocker, nothing to re-diagnose.

## Checkpoint status — 2026-08-12, later still (non-dashboard parallel work: test coverage for a real pre-trading checklist gap)

While the runtime-wiring step above waits on the operator, closed a real, previously-untested item from this file's own "Verification before commissioning checkpoint" list: *"failure of OneCLI or Mission Control fails safely and does not create a path to unauthorized live trading."* This was previously true by code inspection only — `check_broker_reachable()` (`src/api/broker_reads.py`) and `/health`'s outer exception guard (`src/api/routes_live.py`) both already fail safely by design, but had no direct test exercising their failure paths; only the "healthy" path was covered (`test_api_contract.py`'s `stub_broker` fixture hardcodes `check_broker_reachable: lambda: True`).

Added three tests to `tests/test_api_contract.py`, following the file's existing monkeypatch conventions exactly (patching at each module's own import namespace, per its documented convention):
- `check_broker_reachable()` returns `None` when credentials are empty (not configured, distinct from configured-but-down).
- `check_broker_reachable()` returns `False`, not an exception, when `get_account()` raises (simulates a credential-gateway outage — directly relevant to the OneCLI dependency just added).
- `/health` still returns `200` with `broker_reachable: None` even if `check_broker_reachable()` itself raised unexpectedly (defense-in-depth for the outer guard, in case a future change ever broke that function's own never-raise invariant).

Also ran, as routine verification rather than a separate task: full test suite fresh from a clean venv (`1561 passed, 0 failed` — the accepted `1558` baseline plus these 3 new tests, no regressions), and a repo-wide + full-branch-history scan for accidentally-committed secret material (clean — the one pattern match is a pre-existing synthetic test fixture in `tests/test_base_agent.py`, not a real credential).

No trading logic, dashboard/visualization work, or `qamc`-side changes — stayed strictly within Mission Control's existing read-only, non-critical boundary per `.claude/rules/mission-control-api.md`.

## Checkpoint status — 2026-08-12, later still (acceptance automated; four commissioning-adjacent gaps closed)

Re-confirmed first, not assumed: `/health` still reports `broker_reachable: false`, and `dev` still cannot write into `/home/qamc`. The operator-only runtime wiring (step 4 of `ops/onecli/README.md`) is unchanged and remains the single blocker. Everything below is work that did **not** depend on it.

**1. The commissioning checklist above is now executable, not prose.** `ops/commissioning/verify_commissioning.py` runs every criterion in "Verification before commissioning checkpoint" as one read-only command with a real exit code, from `dev`, `qamc`, or `ubuntu`. Design points that matter for trusting its verdicts:
- Each provider is probed through the **same HTTP stack its real caller uses** (OpenRouter/`httpx`, Alpaca/`requests`, FRED/`urllib`). Probing all three with one convenient library would verify a transport QAMC never uses, and would miss exactly the `requests`-ignores-`SSL_CERT_FILE` class of misconfiguration this file already records.
- Credential injection is proven by **difference**, not by an absolute status code: the same fake placeholder credential must be rejected direct and accepted through the gateway. Both legs succeeding is a FAIL (either the endpoint doesn't validate credentials, or a real key leaked client-side), never a pass.
- `SKIP` is first-class and never fails the run, so a check that genuinely can't be evaluated from the current account says so. The trading-timer check `SKIP`s when the account has no `quant-agent` systemd units at all — "looked in the wrong place and found nothing" is not evidence that the runtime's timers are off.
- No credential reaches stdout: response bodies are streamed and discarded unread, and the gateway agent token is redacted.

Run live this pass, it reproduced the manual evidence table exactly (OpenRouter `401`→`200`, Alpaca trading `401`→`200`, Alpaca data `401`→`200`, FRED `400`→`200`), confirmed OneCLI and Mission Control loopback-only, and correctly reported `broker_reachable` FAIL for the pending wiring. The three-round manual diagnosis is now a one-command regression check.

**2. `Alpaca Paper only` is enforced in code.** It was prose in three governance files with zero code enforcement: `alpaca.paper` is the real switch (`alpaca-py` turns it into the endpoint choice), so one token in `settings.yaml` could have pointed the whole decision chain at a live account with nothing to notice. `AlpacaConfig` now fails closed on `paper != true` and on a non-paper `base_url` (which nothing reads today, so a live value there would have misled every future reader rather than actually trading live). Deliberately no env-var escape hatch — authorizing live trading should be a reviewed code change, and deleting the guard's tests *is* that decision, in the open. No behavior change for the accepted config.

**3. Two real coverage gaps on the credential-dependent read paths.** `src/api/broker_reads.py` — the module a gateway outage hits first, whose whole job is to degrade to an `error` field instead of a 500 — was at **28%** line coverage, because the route tests rightly stub it out. Now 97%. `get_latest_price` / `get_bars` / `open_buy_notional` in `src/execution/broker.py` were entirely uncovered; they are the paths that hit `data.alpaca.markets`, the separate host that needed the `*.alpaca.markets` wildcard fix. Module 70% → 83%. One assumption was corrected by the tests rather than asserted: an order object whose every attribute access raises is *not* skipped by `read_orders` — it degrades to a row of nulls, because `_order_to_dict` guards each field individually and never raises as a whole.

**4. The trading-engine half of "OneCLI failure must not create a path to unauthorized trading."** Previously only the Mission Control half was covered. Five tests now drive a real morning run with agents unavailable — PM, AI Risk Manager, all nine at once (the true gateway-down shape), and broker-down-with-credentials-fine — asserting no order is submitted, plus a control case that *does* trade so the others cannot pass vacuously. Observed and pinned for the record: an agent outage propagates out of `run_morning()` rather than returning a structured error result. That is fail-closed and is existing accepted behavior, so it was pinned, not changed.

Zero `src/` changes except the paper-only guard in `src/config.py`. No trading/risk semantics, no dashboard work, no `qamc`-side changes, no new services or dependencies. Full suite **1659 passed, 0 failed** (1561 baseline + 98 new tests).

Engineering note for the next session: `dev` had no `pip`/`ensurepip` (PEP 668, no sudo), so the venv is bootstrapped via `get-pip.py --user --break-system-packages` + `virtualenv` into `~/.local`. Nothing system-wide was changed.

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
