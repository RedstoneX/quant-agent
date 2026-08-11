# QAMC Credential Proxy

Keeps real `OPENROUTER_API_KEY` / `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `FRED_API_KEY` values out of the `qamc` and `dev` accounts entirely. See `../../docs/architecture/CREDENTIAL_PROXY.md` for the full design rationale and empirical verification — this file is the operator runbook.

Everything below runs as the `ubuntu` account (needs `sudo`) or, where marked, must be typed directly by the operator over SSH — never through `dev`, Claude Code, or chat.

**This code went through one independent-review round.** Two BLOCKERs were found and fixed before this runbook was written: (1) a request-body relay bug that could hang on ordinary POST traffic — Alpaca orders, OpenRouter completions — depending on TCP write timing; (2) the proxy had no caller authentication, so any local process (including `dev` itself) could use it exactly as if it held the real credentials, which defeated the point. Both were fixed and empirically re-verified — see `docs/architecture/CREDENTIAL_PROXY.md`'s "Independent review" section. **Do not skip step 3's auth-token requirement below** — the proxy now refuses to start without one.

## 1. Get this code onto the VPS

`dev` cannot write into `ubuntu`'s or `credproxy`'s home directories, so pull the pushed branch to wherever `ubuntu` will run the setup from:

```bash
git clone git@github.com-quant-agent:RedstoneX/quant-agent.git /tmp/qamc-ops-checkout
cd /tmp/qamc-ops-checkout
git checkout claude/vps-deployment-hardening-q3f7k2
```

## 2. Provision the credproxy account, CA, service, and auth token (one command)

```bash
bash ops/credential-proxy/setup.sh
```

This creates the `credproxy` system account, generates its internal CA, publishes the CA's public cert to `/etc/credential-proxy-ca.pem` (world-readable, no secret material), generates a random `_proxy_auth_token` and installs it into `routes.json`, and installs `gateway.py` + the systemd unit — but does **not** start the service and does **not** fill in any of the four real trading/LLM secrets. **The script prints the generated auth token at the end — copy it, you'll need it in step 4.**

## 3. Enter the real secrets — operator only, directly, never through chat

```bash
sudo -u credproxy nano /home/credproxy/routes.json
```

Replace all four `REPLACE-WITH-REAL-VALUE` entries under `"_secrets"` with the real values. Leave `_proxy_auth_token` as `setup.sh` generated it. Save. The file is already `chmod 600`, owned by `credproxy` only — if it's ever anything looser, the proxy refuses to start rather than run with a world-readable secrets file.

## 4. Start the proxy

```bash
sudo systemctl enable --now credential-proxy.service
sudo systemctl status credential-proxy.service --no-pager
```

## 5. Point qamc at it — via `.env`, covering both Mission Control AND the trading engine

Earlier drafts of this runbook only patched `quant-agent-api.service` (Mission Control's read-only health-check process) and missed that the six trading-mode systemd units source `.env` directly via bash (`scripts/run_if_et_window.sh`: `set -a; source .env; set +a`) — a completely separate consumption path with no relationship to `quant-agent-api.service`'s environment. Patching only the former would have left the actual trading engine with no path to real credentials at all. Fixed by adding these to `.env` itself instead — `.env` is already this project's single source of truth for every other secret, and both consumption paths (systemd's presumed `EnvironmentFile=` for Mission Control, and the trading scripts' `source .env`) pick up plain `KEY=VALUE` lines from the same file without needing shell variable interpolation (which systemd's `EnvironmentFile=` doesn't support — this is why the token is written as a literal value baked into the URL below, not a separate variable reference).

`dev` cannot write into `/home/qamc`, so this edit must be applied by whoever has `qamc` access. Add to `/home/qamc/quant-agent/.env` (replacing `<TOKEN>` with the value `setup.sh` printed in step 2):

```
HTTPS_PROXY=http://credproxy:<TOKEN>@127.0.0.1:10255
SSL_CERT_FILE=/etc/credential-proxy-ca.pem
REQUESTS_CA_BUNDLE=/etc/credential-proxy-ca.pem
```

No change is needed to the existing four placeholder credential values in `.env` — they already satisfy QAMC's non-empty config validation, and the proxy replaces them in flight regardless of what placeholder string they hold. Then, as `qamc`:

```bash
systemctl --user daemon-reload
systemctl --user restart quant-agent-api.service
systemctl --user status quant-agent-api.service --no-pager
curl -s http://127.0.0.1:8800/health
```

`/health` should still return 200 with `db_reachable:true`. `broker_reachable` should flip from `false` to `true` once real Alpaca paper credentials are flowing through the proxy — that's the signal the whole chain is live for Mission Control. The trading engine's six systemd timer units already source the same `.env` and will pick this up on their next invocation — **this alone does not enable them**; they remain installed-but-disabled exactly as before, per the standing "no trading timers until paper UAT is ready" boundary. Enabling them is a separate, later, explicit decision.

## Verifying without touching real secrets

`ops/credential-proxy/gateway.py` was validated end-to-end against a disposable local test harness with fake credentials — CONNECT handling, dynamic cert generation, header injection for Alpaca's two-header case, query-param injection for FRED, `httpx`/`requests`/`urllib` compatibility, the `Proxy-Authorization` token check (missing/wrong token → 407; correct token → success, confirmed for all three HTTP libraries), and a request whose headers+body arrive in a single TCP write (the exact condition that triggered the body-relay bug an independent reviewer caught) sent through cleanly with the body content verified byte-for-byte on the other side. See `docs/architecture/CREDENTIAL_PROXY.md` for the specific results.

## Rollback

Remove the three lines from `.env`, restart `quant-agent-api.service`. QAMC returns to its current placeholder-only, non-broker-connected state. Nothing about `qamc`'s own files or trading logic was touched by this integration, so rollback is just removing three lines from one file.
