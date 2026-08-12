# Credential Delivery — Accepted Architecture (OneCLI)

Status: **accepted, commissioned, and verified — 2026-08-12.** This is the durable architecture reference for how QAMC obtains real credentials. See `docs/STATE.md` for current authorization and why the custom proxy from commit `2207b0b74287101ea65ce79782081e51a27420ba` is rejected architecture and must not be revived.

## OneCLI Credential Gateway

- OneCLI is the credential delivery layer for QAMC. It runs under Docker on the VPS, administered by `ubuntu` (see `ops/onecli/README.md`); `qamc` and `dev` are never added to the `docker` group and cannot reach the Docker socket.
- Secrets are stored only in OneCLI, never duplicated in QAMC's own configuration. QAMC's `.env` and `config/settings.yaml` hold placeholder values only.
- Agent access uses explicit secret grants (`secretMode: "selective"` on the Default Agent). Creating a secret does not automatically make it available to an agent — granting it is a separate step.
- The gateway (port `10255`) matches outbound requests to a secret by destination host/path and injects the real credential (header or query parameter) before forwarding; the dashboard (port `10254`) manages secrets/agents/grants. Both bind `127.0.0.1` only.
- QAMC's consuming code needs no awareness of any of this: `src/agents/base.py`'s OpenRouter branch, `src/execution/broker.py`'s `AlpacaBroker`, and `src/data/macro.py`'s `MacroDataProvider` all construct their SDK clients (`openai`/`httpx`, `alpaca-py`/`requests`, `fredapi`/`urllib`) with no custom session/opener, so each already inherits its library's default environment-driven proxy/CA trust. Zero `src/` or `config/` changes were needed to integrate any of the four credentials.
- Client-side wiring is three environment variables in `/home/qamc/quant-agent/.env` (operator-only — `dev` cannot write into `/home/qamc`): `HTTPS_PROXY` (`http://x:<agent-token>@127.0.0.1:10255` — note `127.0.0.1`, not the `host.docker.internal` OneCLI's own `GET /api/container-config` returns by default, which only resolves inside a Docker container and not for QAMC's bare `qamc`-account processes), `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` (both pointed at OneCLI's gateway CA cert — `requests`, Alpaca's transport, does not honor `SSL_CERT_FILE` alone).

## Configured Providers

**OpenRouter** — LLM provider credential for all 9 agents. Header-based: `Authorization: Bearer {value}`, host `openrouter.ai`.

**FRED** — economic data. URL query-parameter injection, not a header: parameter `api_key`, host `api.stlouisfed.org`.

**Alpaca** — paper trading credentials. Two headers, both required together: `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. Values must be injected raw (`{value}`) — **not** `Bearer {value}`; Alpaca's headers are not OAuth-style. Host coverage must be `*.alpaca.markets` (a leading-subdomain wildcard), since QAMC calls both `paper-api.alpaca.markets` (trading) and `data.alpaca.markets` (market/historical data) as distinct hosts. Path pattern must stay blank/unset — Alpaca's API spans multiple paths (`/v2/account`, `/v2/orders`, `/v2/stocks/...`, etc.), and a path pattern narrower than that will block injection on paths it doesn't happen to match.

## Validation

All four credentials were verified working end-to-end through the OneCLI gateway, using obviously-fake placeholder credentials sent by the client and comparing gateway-routed vs. direct requests against endpoints that actually validate the credential (not endpoints that respond regardless of auth). Every response body was discarded; no real credential value was ever read, logged, or held by `dev`.

- OpenRouter connectivity verified through the OneCLI gateway.
- FRED connectivity verified through the OneCLI gateway.
- Alpaca connectivity (both the trading host and the market-data host) verified through the OneCLI gateway, after the operator resolved credential-routing issues by correcting grants, host scope, and header value formatting in OneCLI directly.

Remaining step: apply the `.env` wiring above to `/home/qamc/quant-agent/.env` and confirm `quant-agent-api.service`'s `/health` reports `broker_reachable: true`. Trading timers remain disabled independent of this.

## OneCLI install requirements (for reference)

`github.com/onecli/onecli`'s documented Quick Start install script unconditionally requires Docker + a running daemon + Docker Compose, and deploys PostgreSQL via Docker Compose (not an embedded database). This is why commissioning required a privileged `ubuntu` action rather than something `dev` could complete alone — see `ops/onecli/README.md`.

## HTTP-stack proxy/CA behavior (why the three env vars above are correct and sufficient)

- **`httpx`** (OpenRouter, via the `openai` SDK): honors `HTTPS_PROXY`; CA trust via `SSL_CERT_FILE`.
- **`requests`** (Alpaca, via `alpaca-py`): honors `HTTPS_PROXY`; CA trust via `REQUESTS_CA_BUNDLE` only — does **not** honor `SSL_CERT_FILE`.
- **`urllib`** (FRED, via `fredapi`): honors `HTTPS_PROXY`/`https_proxy`; CA trust via `SSL_CERT_FILE` (OpenSSL-level).
