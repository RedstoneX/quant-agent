# Commissioning upstream OneCLI — operator runbook

This is the accepted credential-management layer for QAMC (see `docs/STATE.md` / `docs/WORK.md`). It replaces the rejected custom proxy from commit `2207b0b74287101ea65ce79782081e51a27420ba`, which must not be revived.

Everything below runs as `ubuntu` (needs `sudo`) — `dev` has neither Docker nor passwordless sudo, and per the accepted account model, `qamc`/`dev` should never be added to the `docker` group either (docker-group membership is root-equivalent on this host, which would collapse the isolation those accounts exist to provide). OneCLI's own Docker stack binds `127.0.0.1` only by default — nothing here is publicly exposed.

## 1. Install Docker Engine + Compose plugin (official apt repository method)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

## 2. Bring up OneCLI's own docker-compose stack (no custom code — the upstream repo's own compose file)

```bash
sudo mkdir -p /opt/onecli
sudo git clone https://github.com/onecli/onecli.git /opt/onecli
cd /opt/onecli
sudo docker compose -f docker/docker-compose.yml up -d --wait
```

This starts two containers: `postgres` (PostgreSQL 18, OneCLI's own data store) and `onecli` (the app: dashboard on port 10254, credential gateway on port 10255). Both bind `127.0.0.1` only by default (`ONECLI_BIND_HOST` in the compose file) — no `.env` file or extra configuration is required for that; it is the compose file's own default, not something this runbook adds.

Optional hardening: the Postgres password defaults to `onecli` (upstream's own default) if not overridden. Since Postgres itself also binds `127.0.0.1` only, this is low-risk, but if you want to set a stronger one, create `/opt/onecli/.env` before step 2 with `POSTGRES_PASSWORD=<your value>` — no code change needed, the compose file already reads it.

## 3. Verify it's running and private

```bash
sudo docker compose -f /opt/onecli/docker/docker-compose.yml ps
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:10254
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:10255
```

Both should respond (2xx/3xx/401, not connection-refused). Confirm neither port is reachable from outside the VPS — e.g. `sudo ss -tlnp | grep -E '10254|10255'` should show `127.0.0.1:10254`/`127.0.0.1:10255`, not `0.0.0.0` or the public IP.

## 4. Wire `qamc` to the gateway and restart Mission Control

By this point OneCLI is running, all four Custom Secrets exist and are granted to the Default Agent (dashboard steps — see `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md` for the exact per-provider configuration), and credential delivery has been verified working. This step is the only thing left: point `qamc`'s existing placeholder credentials at the gateway. `dev` cannot do this — no write access to `/home/qamc`, no access to `qamc`'s `systemd --user` session.

Everything below runs **as `qamc`**. Open the session with:

```bash
sudo -u qamc -i
```

### Files this step changes

| Path | Change |
|---|---|
| `/home/qamc/quant-agent/onecli-gateway-ca.pem` | created (gateway CA, mode `600`) |
| `/home/qamc/quant-agent/.env` | 3 lines added |

Nothing else. The four provider credentials in `.env` (`OPENROUTER_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FRED_API_KEY`) **stay exactly the placeholders they already are** — the real values live only in OneCLI. No systemd unit files change; no timer is enabled.

### 4a. Write the CA certificate

```bash
cd /home/qamc/quant-agent
curl -s http://127.0.0.1:10254/api/container-config \
  | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["caCertificate"])' \
  > onecli-gateway-ca.pem
chmod 600 onecli-gateway-ca.pem
head -1 onecli-gateway-ca.pem
```

Expected output:

```
-----BEGIN CERTIFICATE-----
```

If it prints nothing, OneCLI is not answering — stop and check `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:10254` (expect `200`).

### 4b. Print the three lines to add

```bash
curl -s http://127.0.0.1:10254/api/container-config | python3 -c '
import json, sys
p = json.load(sys.stdin)["env"]["HTTPS_PROXY"].replace("host.docker.internal", "127.0.0.1")
ca = "/home/qamc/quant-agent/onecli-gateway-ca.pem"
print(f"HTTPS_PROXY={p}\nSSL_CERT_FILE={ca}\nREQUESTS_CA_BUNDLE={ca}")'
```

Expected output — three lines, with a real `aoc_…` token in place of `<token>`:

```
HTTPS_PROXY=http://x:<token>@127.0.0.1:10255
SSL_CERT_FILE=/home/qamc/quant-agent/onecli-gateway-ca.pem
REQUESTS_CA_BUNDLE=/home/qamc/quant-agent/onecli-gateway-ca.pem
```

Two things to confirm before continuing: the host reads `127.0.0.1` (**not** `host.docker.internal`, which resolves only inside a container, not for `qamc`'s bare processes), and both CA lines are present (`requests`, Alpaca's transport, does not honor `SSL_CERT_FILE` — it needs `REQUESTS_CA_BUNDLE`).

### 4c. Append them to `.env`

Append those three lines to `/home/qamc/quant-agent/.env`, editing nothing else. Then confirm the file is intact:

```bash
grep -cE '^(HTTPS_PROXY|SSL_CERT_FILE|REQUESTS_CA_BUNDLE)=' .env
grep -E '^(OPENROUTER_API_KEY|ALPACA_API_KEY|ALPACA_SECRET_KEY|FRED_API_KEY)=' .env
```

Expected: `3`, followed by the four credential lines still showing their **placeholder** values.

### 4d. Restart Mission Control

```bash
systemctl --user daemon-reload
systemctl --user restart quant-agent-api.service
systemctl --user status quant-agent-api.service --no-pager
```

Expected: `Active: active (running)`.

Only this service restarts. The trading engine is not a persistent process — it sources `.env` fresh on each scheduled invocation (`scripts/run_if_et_window.sh`), and its timers stay installed-but-disabled either way. **This step does not enable trading.**

### 4e. Verify

```bash
curl -s http://127.0.0.1:8800/health
```

Expected: `200` with `"db_reachable":true`, `"paper":true`, and `"broker_reachable":true` — the flip from `false` to `true` is the objective signal that the whole credential chain is live.

Then the full acceptance check. It covers the entire commissioning checklist in `docs/WORK.md` and exits non-zero on any failure.

**Acceptance runs on two accounts, and that is deliberate — it is the account boundary, not a limitation.** Three checks can only be evaluated from the runtime account (startup validation with real credentials, the runtime CA environment variables, and trading-timer state, which lives in `qamc`'s own `systemd --user` session). One check is only *meaningful* from a non-runtime account: "the runtime's credentials are unreadable off-account" proves nothing when run as the account that owns them. No single login can evaluate all four. Acceptance is the **union of both runs**.

```bash
# 1. runtime account — the three runtime-only checks
sudo -u qamc -i
cd /home/qamc/quant-agent && python3 ops/commissioning/verify_commissioning.py --live

# 2. dev account — the isolation check
cd /home/dev/projects/quant-agent && .venv/bin/python ops/commissioning/verify_commissioning.py --live --from-onecli
```

`--from-onecli` is required on the `dev` run and only there. The wiring checks read `HTTPS_PROXY` from the process environment, which is exactly right on the runtime account — that env IS the thing being verified — but `dev` deliberately has no such wiring, so without the flag the run FAILs on `gateway proxy configured` for the wrong reason. With it, `dev` resolves the gateway from the live OneCLI instance for the duration of the run, and the check that would have been a false alarm correctly reports SKIP with `as qamc` against it.

Expected from each: `COMMISSIONING ACCEPTANCE: PASS` and exit code `0`, followed by an `ACCOUNT COVERAGE:` line. Accept only when **both** runs exit `0` and each reports `ACCOUNT COVERAGE: complete` — a partial run names the account still needed and the checks it owes, so a green summary from one login is never mistaken for full coverage. `--live` additionally completes one real read per provider through QAMC's own clients.

### The tool is not in the runtime checkout yet

`ops/commissioning/` is tracked in this repository, so the runtime gets it exactly the way it gets every other file — by updating its checkout. If `verify_commissioning.py` is missing under `/home/qamc/quant-agent`, that checkout is simply on a commit that predates it (the tooling landed on a Claude branch and reaches `main` through the normal review/merge path).

```bash
sudo -u qamc -i
cd /home/qamc/quant-agent && git pull && ls ops/commissioning/verify_commissioning.py
```

Do **not** copy the file across accounts by hand. That would put an untracked, silently divergent copy inside the runtime — the acceptance tool is only worth trusting if it is the reviewed version, and a hand-placed copy also breaches the `dev`/runtime separation the whole credential architecture rests on. The script needs no dev-only dependency: it uses the standard library plus `yaml`, and reaches for `httpx`/`openai`/`alpaca-py`/`fredapi` only inside guarded imports that degrade to `SKIP`, so it runs under the runtime's own virtualenv unchanged.

### If `broker_reachable` is still `false`

Run the verifier anyway — it distinguishes the causes. Most likely: `.env` not saved, the service restarted before the file was written, or `host.docker.internal` left in the proxy line.

## Rollback

```bash
cd /opt/onecli && sudo docker compose -f docker/docker-compose.yml down
```

Removes the containers; `pgdata`/`app-data` volumes persist unless `-v` is added. QAMC itself is untouched by this — nothing in `qamc`'s `.env` points at OneCLI until the later wiring step above happens.
