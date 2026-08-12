# Credential delivery — empirical HTTP-stack compatibility evidence

Status: reference evidence only, not an architecture document. See `docs/STATE.md` / `docs/WORK.md` for the accepted credential architecture (upstream OneCLI) and for why the custom proxy from `2207b0b74287101ea65ce79782081e51a27420ba` is rejected and must not be revived.

## What this preserves

Regardless of which `HTTPS_PROXY`-based credential-injection mechanism ends up in front of QAMC (upstream OneCLI's gateway, in the accepted direction), the following facts about QAMC's actual dependency code were established empirically — with a disposable local test harness (self-signed CA, a fake HTTPS provider mimicking each real service's exact header/query-param pattern) against real QAMC code paths, not assumptions — and remain true:

- **OpenRouter / `httpx`** (the `openai` SDK's transport, used by all 9 agents): honors `HTTPS_PROXY` by default; CA trust via `SSL_CERT_FILE`.
- **Alpaca / `requests`** (`alpaca-py`'s actual `Session()`, confirmed from source): honors `HTTPS_PROXY` by default, but **does not honor `SSL_CERT_FILE`** — only `REQUESTS_CA_BUNDLE` establishes trust for this library. Two-header credential injection (`APCA-API-KEY-ID` + `APCA-API-SECRET-KEY`) is required — a single bearer token is not sufficient.
- **FRED / `urllib`** (`fredapi`'s actual transport, confirmed from source — not `requests`): honors both `HTTPS_PROXY` and lowercase `https_proxy`; CA trust via `SSL_CERT_FILE` (OpenSSL-level). Credential injection is via query parameter (`?api_key=`), not a header.

**Conclusion that stays true regardless of mechanism:** QAMC's `.env` (or whatever env OneCLI's gateway is invoked under) will need to export **both** `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` (not just one) for all three of QAMC's HTTP stacks to trust the proxy, and the credential-injection mechanism must support header injection (2 distinct header names for Alpaca) and query-param injection (FRED), not header injection alone. This does not need to be re-derived when OneCLI is provisioned — it should be used directly to write the commissioning verification checks.

## OneCLI's own install requirements (verified against upstream, 2026-08-11)

`github.com/onecli/onecli`'s documented Quick Start (`curl -fsSL https://onecli.sh/install | sh`) fetches an install script that checks for `docker`, a running Docker daemon, and `docker compose`, exiting immediately if any are missing, and deploys PostgreSQL via Docker Compose (not an embedded database) for that path. The `dev` account has neither Docker nor passwordless `sudo`, which is why this is a privileged `ubuntu` action rather than something `dev` can complete alone.
