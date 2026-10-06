#!/usr/bin/env bash
# Run the broker conformance check as a bounded, transient qamc user service.
# No secret enters argv or the environment: systemd copies the qamc-owned source
# files into the unit's private credential directory and removes that directory
# when the one-shot unit is collected.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
RUNTIME_ROOT="/home/qamc/quant-agent"
CREDENTIAL_ROOT="/home/qamc/credentials/rehearsal"
QAMC_UID="$(id -u qamc)"

if [[ "$PROJECT_ROOT" != "$RUNTIME_ROOT" ]]; then
    echo "STOP: live broker conformance must run from the deployed runtime checkout" >&2
    exit 1
fi

for name in alpaca_api_key alpaca_secret_key; do
    if ! sudo -n -u qamc test -s "${CREDENTIAL_ROOT}/${name}"; then
        echo "STOP: rehearsal credential file ${name} is absent or empty" >&2
        exit 1
    fi
done

exec sudo -n -u qamc env XDG_RUNTIME_DIR="/run/user/${QAMC_UID}" \
    systemd-run --user --wait --collect --pipe \
    --unit=quant-agent-rehearsal-conformance \
    --property="WorkingDirectory=${RUNTIME_ROOT}" \
    --property="LoadCredential=alpaca_api_key:${CREDENTIAL_ROOT}/alpaca_api_key" \
    --property="LoadCredential=alpaca_secret_key:${CREDENTIAL_ROOT}/alpaca_secret_key" \
    --setenv=QAMC_REHEARSAL=1 \
    --setenv=QAMC_SESSION_IDENTITY=rehearsal \
    /usr/bin/timeout --kill-after=30 600 \
    "${RUNTIME_ROOT}/.venv/bin/python" -m ops.rehearsal.conformance --live "$@"
