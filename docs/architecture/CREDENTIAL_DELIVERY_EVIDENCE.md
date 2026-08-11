# Credential delivery — investigation evidence (not an accepted architecture)

Status: **no credential delivery mechanism is implemented or accepted.** This file preserves empirical findings only. It does not authorize building anything.

## Why this file exists

Commit `2207b0b` built a custom stdlib HTTPS forward proxy (`ops/credential-proxy/`) as a substitute for the real OneCLI product after OneCLI's install was found to require Docker. `CLAUDE.md` was subsequently updated (`7b51efd`, operator) with a HARD architectural-authority rule: Claude may not build a substitute service/proxy/vault/gateway/database when an approved external product is blocked — that requires stopping and asking, not inventing an alternative. The custom proxy was therefore reverted in full (`ops/credential-proxy/` removed, `.env.example` pointer removed) as out-of-process architecture. This document keeps the parts of that work that are genuine, reusable, empirical facts about this repository — independent of which credential-delivery mechanism is eventually approved.

## OneCLI investigation (re-verified 2026-08-11, not taken on trust from the prior session)

[github.com/onecli/onecli](https://github.com/onecli/onecli) is a real, actively maintained (releases through v1.45.0, 2026-07-31) open-source credential gateway matching the intended use case: agents call a local proxy with placeholder keys; the proxy injects real credentials at the network layer.

Re-fetched directly from upstream today, not summarized secondhand:

- The documented Quick Start (`curl -fsSL https://onecli.sh/install | sh`) runs `onecli.sh`'s actual install script, which:
  - checks for `docker`, a running Docker daemon, and `docker compose` — and **exits 1 immediately if any are missing**, with no fallback;
  - deploys PostgreSQL via `docker compose`, not an embedded/local-file database, for this install path.
- The "from source" local-dev path also expects Docker for Postgres (`pnpm db:up`) unless an external `DATABASE_URL` is supplied — which would itself mean depending on a new externally-hosted database service.
- There is no documented non-Docker, no-new-infrastructure install path for this VPS's actual constraints.

Re-confirmed empirically, live, on the actual `dev` account on `vps-37b5f875` (not assumed from the prior session's notes):

```
$ command -v docker        → not found
$ sudo -n true              → "a password is required" (no passwordless sudo)
$ command -v psql           → not found
```

`dev` cannot install Docker, Postgres, or any other system package itself. Standing up real OneCLI from here requires either (a) an operator/sudo-privileged Docker install on this VPS, or (b) pointing OneCLI at an external managed Postgres — both are infrastructure decisions outside routine implementation authority under the current HARD RULE, not something to route around.

## Empirical HTTP-stack proxy-compatibility findings (retained — reusable regardless of mechanism)

These facts about QAMC's actual dependency code were established with a disposable local test harness (self-signed CA, fake HTTPS provider mimicking each real service's exact header/query-param pattern) and hold true for **any** `HTTPS_PROXY`-based credential-injection mechanism, including a correctly-provisioned real OneCLI gateway:

- **OpenRouter / `httpx`** (the `openai` SDK's transport, used by all 9 agents post-OpenRouter-migration): honors `HTTPS_PROXY` by default; CA trust via `SSL_CERT_FILE`.
- **Alpaca / `requests`** (`alpaca-py`'s actual `Session()`, confirmed from source): honors `HTTPS_PROXY` by default, but **does not honor `SSL_CERT_FILE`** — only `REQUESTS_CA_BUNDLE` establishes trust for this library. Two-header credential injection (`APCA-API-KEY-ID` + `APCA-API-SECRET-KEY`) is required.
- **FRED / `urllib`** (`fredapi`'s actual transport, confirmed from source — not `requests`): honors both `HTTPS_PROXY` and lowercase `https_proxy`; CA trust via `SSL_CERT_FILE` (OpenSSL-level). Credential injection is via query parameter (`?api_key=`), not a header.

**Conclusion that stays true regardless of the eventual mechanism:** whatever proxy ends up in front of these three env vars, QAMC's `.env` will need to export **both** `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` (not just one) for all three HTTP stacks to trust it, and the mechanism must support header injection (2 cases) and query-param injection (1 case), not header injection alone. This does not need to be re-derived when a real mechanism is provisioned.

## What was reverted and why

- `ops/credential-proxy/` (the custom stdlib proxy, its systemd unit, setup script, operator runbook) — removed. It was a working, independently-reviewed, security-hardened implementation, but it was a durable custom service built as a substitute for a blocked approved product, which the new architectural-authority rule does not permit without explicit approval.
- The `.env.example` pointer comment referencing it — removed.
- Nothing in `src/` was ever touched by the reverted work, so no functional revert was needed there.

## Current state

No mechanism exists to get real `OPENROUTER_API_KEY` / `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `FRED_API_KEY` values to `qamc` without `qamc` or `dev` holding them directly, or without provisioning real OneCLI (which needs an infrastructure decision this file does not make). This is an open architectural fork — see `docs/WORK.md` for the exact question posed to the operator.
