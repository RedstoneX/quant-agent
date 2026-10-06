#!/usr/bin/env bash
# One disposable secondary-Paper morning run. No installed unit or desk restart.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT=/home/qamc/quant-agent
REHEARSAL_KEYS=/home/qamc/credentials/rehearsal
PRIMARY_KEYS=/home/qamc/credentials
QAMC_UID="$(id -u qamc)"
QAMC_RUN=(sudo -n -u qamc env "XDG_RUNTIME_DIR=/run/user/${QAMC_UID}")
PYTHON="${RUNTIME_ROOT}/.venv/bin/python"

if [[ "$(id -un)" != ubuntu ]]; then
    echo 'STOP: run capture orchestration as the ubuntu engineering account' >&2
    exit 2
fi
REPO_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
MAIN_HEAD="$(git -C "$REPO_ROOT" rev-parse origin/main)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" || "$REPO_HEAD" != "$MAIN_HEAD" ]]; then
    echo 'STOP: capture must run from a clean, verified main checkout' >&2
    exit 2
fi
for name in alpaca_api_key alpaca_secret_key; do
    if ! sudo -n -u qamc test -s "${REHEARSAL_KEYS}/${name}"; then
        echo "STOP: secondary credential file ${name} is absent" >&2
        exit 2
    fi
done
for name in alpaca_api_key alpaca_secret_key; do
    if ! sudo -n -u qamc test -s "${PRIMARY_KEYS}/${name}"; then
        echo "STOP: primary credential file ${name} is absent" >&2
        exit 2
    fi
done

# The Python gate repeats this check after the archive and immediately before
# the broker preflight. All installed QAMC timers must remain parked, including
# intra_check/intra_safety, which bypass the normal cross-session lock.
(cd "$REPO_ROOT" && /home/ubuntu/projects/quant-agent/.venv/bin/python -c \
    'from ops.rehearsal.secondary_preflight import assert_qamc_timers_parked; assert_qamc_timers_parked()')

SCRATCH="$(sudo -n -u qamc mktemp -d -p /home/qamc/.cache/quant-agent secondary-capture.XXXXXXXX)"
git -C "$REPO_ROOT" archive --format=tar HEAD |
    sudo -n -u qamc tar -xf - -C "$SCRATCH"
UNIT_SUFFIX="$(date -u +%Y%m%d%H%M%S)-$$"

# The primary lookup is a separate, read-only transient process. It receives
# no secondary API key and writes only the account-number assertion into the
# private scratch tree. The capture process receives no primary API key.
"${QAMC_RUN[@]}" systemd-run --user --wait --collect --pipe \
    "--unit=qamc-primary-identity-${UNIT_SUFFIX}" \
    "--property=WorkingDirectory=${SCRATCH}" \
    "--property=LoadCredential=alpaca_api_key:${PRIMARY_KEYS}/alpaca_api_key" \
    "--property=LoadCredential=alpaca_secret_key:${PRIMARY_KEYS}/alpaca_secret_key" \
    --setenv=QAMC_SESSION_IDENTITY=desk \
    /usr/bin/timeout --kill-after=5 30 "$PYTHON" \
    -m ops.rehearsal.primary_identity_assertion \
    --scratch-dir "$SCRATCH"

# 1200s is the normal desk wrapper's own session ceiling. The 30s TERM grace
# permits its safety finally-blocks to unwind. Resource limits protect the
# 6-CPU/12-GiB VPS while leaving the owner API responsive.
"${QAMC_RUN[@]}" systemd-run --user --wait --collect --pipe \
    "--unit=qamc-secondary-capture-${UNIT_SUFFIX}" \
    "--property=WorkingDirectory=${SCRATCH}" \
    "--property=MemoryHigh=4G" \
    "--property=MemoryMax=6G" \
    "--property=CPUQuota=300%" \
    "--property=LoadCredential=alpaca_api_key:${REHEARSAL_KEYS}/alpaca_api_key" \
    "--property=LoadCredential=alpaca_secret_key:${REHEARSAL_KEYS}/alpaca_secret_key" \
    "--property=LoadCredential=primary_account_number:${SCRATCH}/primary_account_number" \
    --setenv=QAMC_REHEARSAL=1 \
    --setenv=QAMC_SESSION_IDENTITY=rehearsal \
    "--setenv=QAMC_CAPTURE_SOURCE_SHA=${REPO_HEAD}" \
    /usr/bin/timeout --kill-after=30 1200 \
    /usr/bin/bash "${SCRATCH}/scripts/secondary_capture_entry.sh" "$SCRATCH"

echo "Secondary capture completed; private evidence: ${SCRATCH}/data"
