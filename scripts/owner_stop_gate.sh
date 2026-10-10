#!/usr/bin/env bash
# Owner Stop gate -- the ONE ExecCondition= every desk unit in scripts/systemd
# carries (owner ruling 2026-10-09: Stop = the desk is OFF; nothing reads,
# nothing runs, no AI; positions and the protective stops at the broker kept).
#
# systemd reads ExecCondition= exit codes as: 0 = run the unit, 1..254 = skip
# it cleanly (the unit is NOT marked failed), 255 or a signal = fail. So:
#   exit 1 -> owner Stop in force: the unit is skipped
#   exit 0 -> no Stop: the unit runs
#
# The decision is main.py's own `_owner_stop_in_force` (PR #1671): owner-intent
# pickup, the Stop read through src.owner_flags, and -- only when stopped -- the
# Freeze sweep of resting UNFILLED entries (protective stops kept). It is
# imported, never re-implemented, so the gate and a one-shot main.py run can
# never disagree. Because the gate now skips the session units before main.py
# starts, it is also where that sweep runs while Stopped.
#
# Anything the gate cannot settle (python missing, import error, timeout) runs
# the unit, matching src.owner_flags.stop_in_force: an UNKNOWN read is not a
# Stop, so a broken gate can never silently switch the desk's exits off. The
# session units still re-check Stop inside main.py.
#
# Not set -e: every failure must fall through to "run".
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"
CONFIG="${OWNER_STOP_GATE_CONFIG:-config/settings.yaml}"
UNIT="${1:-unit}"
# The stopped branch makes one bounded broker call (the sweep); 60s is the
# owner-visible delay ceiling on a unit start, not a tuned value.
GATE_TIMEOUT_SEC="${OWNER_STOP_GATE_TIMEOUT_SEC:-60}"

cd "$PROJECT_ROOT" || exit 0

# Same environment the runners give main.py: .env first, then systemd-delivered
# broker credentials win over it (see run_if_et_window.sh for why the order).
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_ROOT}/.env"
    set +a
fi
if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
    if [[ -r "${CREDENTIALS_DIRECTORY}/alpaca_api_key" ]]; then
        ALPACA_API_KEY="$(<"${CREDENTIALS_DIRECTORY}/alpaca_api_key")"
        export ALPACA_API_KEY
    fi
    if [[ -r "${CREDENTIALS_DIRECTORY}/alpaca_secret_key" ]]; then
        ALPACA_SECRET_KEY="$(<"${CREDENTIALS_DIRECTORY}/alpaca_secret_key")"
        export ALPACA_SECRET_KEY
    fi
fi

"$TIMEOUT" "$GATE_TIMEOUT_SEC" "$PYTHON" -c '
import sys
import main
sys.exit(main.OWNER_STOP_EXIT if main._owner_stop_in_force(sys.argv[1]) else 0)
' "$CONFIG" >/dev/null
STATUS=$?

if [[ "$STATUS" -eq 75 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${UNIT} skipped: owner Stop in force, the desk is off" >&2
    exit 1
fi
if [[ "$STATUS" -ne 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ${UNIT}: owner Stop gate could not decide (status ${STATUS}); running the unit" >&2
fi
exit 0
