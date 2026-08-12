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

Run as `qamc` (or `ubuntu` via `sudo -u qamc -i`):

```bash
curl -s http://127.0.0.1:10254/api/container-config   # fetch fresh — don't reuse an old copy
```

From that response: save the `caCertificate` PEM to a local file (e.g. `/home/qamc/quant-agent/onecli-gateway-ca.pem`), and add three lines to the existing `/home/qamc/quant-agent/.env` — do **not** touch `OPENROUTER_API_KEY`/`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`FRED_API_KEY`, which stay exactly the placeholders they already are:

```
HTTPS_PROXY=http://x:<agent-token-from-the-response-above>@127.0.0.1:10255
SSL_CERT_FILE=/home/qamc/quant-agent/onecli-gateway-ca.pem
REQUESTS_CA_BUNDLE=/home/qamc/quant-agent/onecli-gateway-ca.pem
```

(Use `127.0.0.1`, not the `host.docker.internal` the response itself shows — that only resolves inside a Docker container, not for `qamc`'s bare processes.)

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart quant-agent-api.service
systemctl --user status quant-agent-api.service --no-pager
curl -s http://127.0.0.1:8800/health
```

`/health` should still return `200` with `db_reachable: true`; `broker_reachable` should flip from `false` to `true` — the objective signal the whole chain is live. No other service needs restarting: the trading engine isn't a persistent process, it sources `.env` fresh on each scheduled invocation (`scripts/run_if_et_window.sh`), and its timers remain installed-but-disabled regardless of this step. **This step alone does not enable trading** — enabling the timers is a separate, later, explicit decision.

## Rollback

```bash
cd /opt/onecli && sudo docker compose -f docker/docker-compose.yml down
```

Removes the containers; `pgdata`/`app-data` volumes persist unless `-v` is added. QAMC itself is untouched by this — nothing in `qamc`'s `.env` points at OneCLI until the later wiring step above happens.
