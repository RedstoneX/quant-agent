# QAMC Current State

Updated: 2026-08-12

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Stage 3 delivered the read-only browser/iPad Trading Cockpit at `/ui`; accepted verification: **1531 passed, 0 failed** plus desktop/iPad runtime review.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Stage 4 delivered per-candidate specialist evidence + decision-chain drill-down while preserving each specialist's real data scope and existing `decision_id` semantics.
- Stage 5 delivered read-only journal and parameterized forensic search. Stage 4–5 passed external ChatGPT/operator review with **1558 passed, 0 failed** plus committed browser/runtime evidence.
- PR #24 is merged into `main` as merge commit `105cc91a14faebd8a981061b3098eb181b306dda`.
- The permanent frontend-verification requirement under `.claude/rules/frontend-verification.md` remains accepted.
- The OVH VPS runtime architecture remains: `ubuntu` = administration/recovery, `qamc` = isolated QAMC runtime, `dev` = development/Claude Code workspace.
- Mission Control/API is deployed under `qamc`, private/read-only, and trading timers remain disabled pending commissioning.
- OpenRouter routing for all 9 agents is accepted: explicit provider `openrouter`, model `openai/gpt-5.5`, with no model diversification. This uses the already-accepted provider seam.
- The architectural-authority hard rule in `CLAUDE.md` is accepted: Claude may act autonomously inside accepted architecture but must stop at material architectural forks rather than invent replacements.
- VPS baseline security hardening is complete and verified: UFW active with a deny-incoming default (SSH and the `tailscale0` interface explicitly allowed), fail2ban's `sshd` jail active, the previously-staged kernel update applied via a completed reboot, Tailscale confirmed connected with no subnet router or exit node configured, `btop`/`iftop` installed as lightweight operator inspection tools. See `ops/security/vps-hardening-plan.md`.
- OneCLI credential gateway commissioning is complete: all four real credentials (OpenRouter, Alpaca Key ID, Alpaca Secret, FRED) are stored in OneCLI and verified working end-to-end through the gateway. Zero `src/`/`config/` changes were required. See `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` for the accepted architecture and injection configuration per provider.
- Commissioning acceptance is automated: `ops/commissioning/verify_commissioning.py` executes the whole "Verification before commissioning checkpoint" list from `docs/WORK.md` as one read-only, exit-code-bearing command. It is the acceptance evidence run — the manual `curl` sequences it replaces should not be re-derived by hand.
- The provider chain is verified end-to-end ahead of commissioning. `verify_commissioning.py --live` builds the same `openai`/`alpaca-py`/`fredapi` clients the trading engine builds and completes one real read with each; all nine checks pass, including `openai/gpt-5.5` being present in OpenRouter's live catalog, a real completion, the Alpaca SDK's **resolved** endpoint being the paper one, working market data on `data.alpaca.markets`, and a live FRED observation. Only the runtime `.env` wiring remains between this and a working deployment.
- `Alpaca Paper only` is now enforced in code, not only in prose: `AlpacaConfig` fails closed at config load on a non-paper `paper` flag or `base_url`. Removing that guard is the act of authorizing live trading and requires a reviewed change with its own commit.

## Explicitly not accepted

Commit `2207b0b74287101ea65ce79782081e51a27420ba` contains a custom credential-proxy implementation created after OneCLI installation was found to require Docker/root access. That custom proxy, its service, its credential architecture, and its supporting `CREDENTIAL_PROXY.md` are **not accepted QAMC architecture** and must not be provisioned, hardened, extended, deployed, or treated as the current credential solution.

Useful empirical compatibility evidence from that work may be consulted (for example QAMC transport behavior across `httpx`, `requests`, and `urllib`), but the home-grown gateway itself is rejected.

## Authorized now

**Commission QAMC into a real, verified Alpaca Paper deployment using OpenRouter and the upstream-maintained OneCLI product, if OneCLI proves viable in the actual VPS/runtime context.** See `docs/WORK.md` for the active work contract.

Claude may investigate, implement, test, fix, and continue autonomously inside this accepted direction. A requirement for operator-entered secrets or privileged `sudo` is a valid stop boundary. If upstream OneCLI itself proves materially unsuitable, Claude must stop and report the architectural fork; it may not build a substitute credential system.

## Not authorized now

- live trading;
- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- public exposure of QAMC or OneCLI services;
- collapsing `dev` / `qamc` account isolation;
- enabling trading timers before commissioning verification makes that appropriate;
- custom credential gateways/proxies/vaults or other durable replacements for OneCLI without separate architectural approval;
- dedicated dashboard visualization/visual-polish work until the deployed MVP is accepted;
- later learning/write-control stages without authorization;
- unnecessary infrastructure expansion beyond what the approved upstream OneCLI deployment actually requires.

## Handoff

Claude Code operates from `/home/dev/projects/quant-agent` for engineering work. Runtime changes under `/home/qamc` must preserve the accepted account boundary. Real secrets must never pass through chat or Git.

Upstream OneCLI is installed, running privately on the VPS, and commissioned: all four real credentials are stored in it and verified working end-to-end (see `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` for the accepted architecture and per-provider configuration). `dev` has never held or seen a real credential value.

The next engineering action is to wire `qamc`'s `.env` (`HTTPS_PROXY`/`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`, pointed at the OneCLI gateway) — operator-only, since `dev` cannot write into `/home/qamc` — then run `python ops/commissioning/verify_commissioning.py` as `qamc`, which checks that `broker_reachable` flipped true along with every other acceptance criterion in one pass. Trading timers remain disabled until that evidence is reviewed and activation is appropriate.
