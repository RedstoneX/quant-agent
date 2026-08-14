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

On a fresh deployment, this step points `qamc`'s existing placeholder credentials at the gateway. On the current accepted QAMC deployment the wiring already exists; do **not** append duplicate lines to `.env`. The instructions remain here as the canonical rebuild/recovery procedure. `dev` cannot perform this work — it has no write access to `/home/qamc` and no access to `qamc`'s `systemd --user` session.

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

A shell entered through `sudo -u qamc -i` may not receive the environment variables needed to address `qamc`'s lingering `systemd --user` bus. Set them explicitly; this does not change any service configuration:

```bash
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

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

Expected: `200` with `"db_reachable":true`, `"paper":true`, and `"broker_reachable":true` — `broker_reachable:true` is the objective signal that the whole credential chain is live.

Then run the full acceptance check. It covers the commissioning checklist and exits non-zero on any failure.

**Acceptance runs on two accounts, and that is deliberate — it is the account boundary, not a limitation.** Three checks can only be evaluated from the runtime account (startup validation with real credentials, the runtime CA environment variables, and trading-timer state, which lives in `qamc`'s own `systemd --user` session). One check is only meaningful from a non-runtime account: "the runtime's credentials are unreadable off-account" proves nothing when run as the account that owns them. No single login can evaluate all four. Acceptance is the **union of both runs**.

#### Runtime half — as `qamc`

The service and scheduled trading wrapper read `.env`, but an interactive shell does not automatically export it. The verifier must inspect the actual runtime wiring, so load `.env` into this shell without printing its values and use QAMC's runtime virtualenv rather than system Python:

```bash
cd /home/qamc/quant-agent

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

set -a
. ./.env
set +a

.venv/bin/python ops/commissioning/verify_commissioning.py --live
```

Do **not** use `--from-onecli` on the runtime half. That flag creates temporary wiring for a non-runtime account and would bypass the very environment this run is supposed to verify.

#### Development/isolation half — as `dev`

```bash
cd /home/dev/projects/quant-agent
.venv/bin/python ops/commissioning/verify_commissioning.py --live --from-onecli
```

`--from-onecli` is required on the `dev` run and only there. `dev` deliberately has no runtime proxy/CA environment, so the flag supplies a temporary wiring copy for provider checks while still allowing the off-account isolation check to prove that `/home/qamc` is unreadable.

#### How to read the two results

Each run must exit `0` with **zero FAIL results**. A run may legitimately say `COMMISSIONING ACCEPTANCE: PASS (with skipped/warned checks — review them before accepting)` and `ACCOUNT COVERAGE: partial` because checks assigned to the other account are intentionally SKIPped.

Do **not** require either single-account run to say `ACCOUNT COVERAGE: complete`; that is impossible by design. The `qamc` run must defer only the off-account isolation check to `dev`, and the `dev` run must defer the runtime-only checks to `qamc`. Full commissioning acceptance is the **union of the two green runs**.

`--live` additionally completes real read-only provider calls through QAMC's own clients; the LLM portion makes one tiny completion per distinct policy model.

### If the verifier is missing from a runtime checkout

`ops/commissioning/` is tracked in this repository, so the runtime gets it exactly the way it gets every other reviewed file — by updating its checkout to accepted `main`.

```bash
sudo -u qamc -i
cd /home/qamc/quant-agent
git fetch origin
git checkout main
git pull --ff-only origin main
ls ops/commissioning/verify_commissioning.py
```

Do **not** copy the file across accounts by hand. That would put an untracked, silently divergent copy inside the runtime and breach the `dev`/runtime separation the acceptance tool is meant to verify. Run the tool with the runtime's existing `.venv/bin/python`, not system `python3`.

### If `broker_reachable` is still `false`

Run the verifier anyway — it distinguishes the causes. Most likely: `.env` not saved, the service restarted before the file was written, or `host.docker.internal` left in the proxy line.

## Rollback

```bash
cd /opt/onecli && sudo docker compose -f docker/docker-compose.yml down
```

Removes the containers; `pgdata`/`app-data` volumes persist unless `-v` is added. QAMC itself is untouched by this — nothing in `qamc`'s `.env` points at OneCLI until the wiring step above happens.
