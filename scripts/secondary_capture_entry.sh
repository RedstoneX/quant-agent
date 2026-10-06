#!/usr/bin/env bash
# Runs only inside the transient qamc capture unit; stdout is private.
set -euo pipefail
umask 077
SCRATCH="${1:?private scratch root required}"
if [[ "$(id -un)" != qamc || -z "${CREDENTIALS_DIRECTORY:-}" ||
      ! -d "$SCRATCH" || "$SCRATCH" == /home/qamc/quant-agent ]]; then
    echo 'STOP: secondary capture unit boundary is absent' >&2
    exit 2
fi
DELIVERED_DIRECTORY="$CREDENTIALS_DIRECTORY"
exec >"${SCRATCH}/capture.log" 2>&1
set -a
# Preserve the desk's existing model/FRED/OneCLI delivery. The Python entry
# clears only inherited Alpaca variables and uses the secondary credential
# files; it bypasses OneCLI for the two Alpaca REST hosts only.
# shellcheck disable=SC1091
source /home/qamc/quant-agent/.env
set +a
if [[ "${CREDENTIALS_DIRECTORY:-}" != "$DELIVERED_DIRECTORY" ]]; then
    echo 'STOP: systemd credential directory changed during environment load' >&2
    exit 2
fi
export QAMC_REHEARSAL=1 QAMC_SESSION_IDENTITY=rehearsal
exec /home/qamc/quant-agent/.venv/bin/python -m ops.rehearsal.live_capture \
    --code-root "$SCRATCH" --max-seconds 1200 --max-bytes 536870912 \
    --min-free-mib 4096 --max-load-per-cpu 1.0
