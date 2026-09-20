#!/usr/bin/env bash
# Log-health report runner — systemd entry point.
#
# Same shape as run_unit_drift_check.sh / run_drift_check.sh, and for the same
# recorded reason: the script raises a Telegram message, and the qamc systemd
# user environment carries zero TELEGRAM_* variables. Without `.env` sourced,
# `TelegramNotifier` disables itself and the report is printed to the journal
# and delivered to nobody.
#
# NOT routed through run_if_et_window.sh. That wrapper exists for the six
# ET-windowed TRADING sessions: it rejects unknown modes by design, and its
# once-per-session marker, cross-mode session lock and 20-minute trading
# timeout all apply to a Python process that talks to the broker. This one
# reads files. The ET awareness it does need is the firing time, and the timer
# gets that from systemd's own IANA zone support (`America/New_York`), which is
# how quant-agent-drift-check and quant-agent-unit-drift already do it.
#
# Arguments are passed through.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 180s: the duration-measuring families read the rotated logs (about 50MB of
# retained history), which is the slowest thing this job does. No network
# beyond one Telegram push, no model call, no broker call.
exec "$TIMEOUT" --kill-after=15 180 "$PYTHON" scripts/log_health_report.py "$@"
