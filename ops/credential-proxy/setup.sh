#!/bin/bash
# One-time provisioning for the QAMC credential proxy.
# Run as a user with sudo (the `ubuntu` account). Never run as `qamc` or `dev`
# — the whole point is that neither of those accounts holds this material.
#
# What this does:
#   1. Creates a dedicated, unprivileged system account `credproxy` that
#      owns the real credentials and the proxy's signing CA. Neither
#      `qamc` nor `dev` can read this account's home directory.
#   2. Generates a private CA (used only to sign short-lived leaf certs
#      for the 4 known upstream hosts, on the fly, at request time).
#   3. Publishes the CA's PUBLIC certificate to a world-readable system
#      path so `qamc` can be told to trust it — the private key never
#      leaves `credproxy`'s home (mode 700).
#   4. Installs gateway.py and the systemd unit, but does NOT start it —
#      routes.json must be filled in with real secrets first (by the
#      operator, directly, never through this script or through chat).
set -euo pipefail

if [ "$(id -u)" -eq 0 ] || [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as a normal user with sudo (e.g. the ubuntu account), not as root directly." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDPROXY_HOME="/home/credproxy"
CA_PUB_PATH="/etc/credential-proxy-ca.pem"

echo "== 1. Creating credproxy system account =="
if id credproxy >/dev/null 2>&1; then
  echo "   credproxy already exists, skipping useradd."
else
  sudo useradd --system --create-home --home-dir "$CREDPROXY_HOME" --shell /usr/sbin/nologin credproxy
fi

echo "== 2. Installing gateway.py =="
sudo install -o credproxy -g credproxy -m 0644 "$SCRIPT_DIR/gateway.py" "$CREDPROXY_HOME/gateway.py"

echo "== 3. Generating the internal signing CA (10y validity — this CA is never publicly trusted, only qamc is told to trust it; document manual rotation if ever needed) =="
sudo -u credproxy mkdir -p "$CREDPROXY_HOME/ca"
sudo -u credproxy chmod 700 "$CREDPROXY_HOME/ca"
if [ -f "$CREDPROXY_HOME/ca/ca-cert.pem" ]; then
  echo "   CA already exists at $CREDPROXY_HOME/ca — leaving it in place. Delete manually to force regeneration."
else
  sudo -u credproxy openssl req -x509 -newkey rsa:4096 \
    -keyout "$CREDPROXY_HOME/ca/ca-key.pem" \
    -out "$CREDPROXY_HOME/ca/ca-cert.pem" \
    -days 3650 -nodes -subj "/CN=QAMC-Credential-Proxy-Internal-CA"
  sudo chmod 600 "$CREDPROXY_HOME/ca/ca-key.pem"
  sudo chmod 644 "$CREDPROXY_HOME/ca/ca-cert.pem"
fi

echo "== 4. Publishing the CA's PUBLIC cert for qamc to trust (private key stays in credproxy's home only) =="
sudo install -o root -g root -m 0644 "$CREDPROXY_HOME/ca/ca-cert.pem" "$CA_PUB_PATH"
echo "   Published to $CA_PUB_PATH (world-readable, contains no secret material)."

echo "== 5. Routes/secrets config + proxy auth token =="
if [ -f "$CREDPROXY_HOME/routes.json" ]; then
  echo "   $CREDPROXY_HOME/routes.json already exists — leaving it in place (including its existing token)."
else
  AUTH_TOKEN="$(openssl rand -hex 32)"
  sudo -u credproxy python3 -c "
import json, sys
with open('$SCRIPT_DIR/routes.example.json') as f:
    data = json.load(f)
data['_proxy_auth_token'] = '$AUTH_TOKEN'
with open('$CREDPROXY_HOME/routes.json', 'w') as f:
    json.dump(data, f, indent=2)
"
  sudo chmod 600 "$CREDPROXY_HOME/routes.json"
  sudo chown credproxy:credproxy "$CREDPROXY_HOME/routes.json"
  echo "   Installed TEMPLATE at $CREDPROXY_HOME/routes.json (chmod 600, credproxy-owned) with a fresh random auth token."
  echo ""
  echo "   *** Operator action required (two parts, both direct — never through chat):"
  echo "   ***  1. Edit $CREDPROXY_HOME/routes.json as the credproxy user and replace every"
  echo "   ***     REPLACE-WITH-REAL-VALUE under \"_secrets\" with the real credential."
  echo "   ***  2. This proxy auth token must ALSO be added to qamc's .env as CREDPROXY_AUTH_TOKEN"
  echo "   ***     — it is not a trading/LLM secret, just the shared value that lets qamc (and"
  echo "   ***     only qamc) authenticate to this proxy. Token (safe to relay once, not a"
  echo "   ***     trading credential, but still treat it as sensitive — anyone with it can use"
  echo "   ***     the proxy):"
  echo "   ***       $AUTH_TOKEN"
fi

echo "== 6. Installing systemd unit (not started yet) =="
sudo install -o root -g root -m 0644 "$SCRIPT_DIR/credential-proxy.service" /etc/systemd/system/credential-proxy.service
sudo systemctl daemon-reload
echo "   Unit installed. Start with: sudo systemctl enable --now credential-proxy.service"
echo "   (only after routes.json has real secrets in it)"

echo ""
echo "Done. Remaining manual steps:"
echo "  1. sudo -u credproxy nano $CREDPROXY_HOME/routes.json   # fill in real secrets"
echo "  2. sudo systemctl enable --now credential-proxy.service"
echo "  3. Apply the qamc-side integration (see ops/credential-proxy/README.md) and restart quant-agent-api.service"
