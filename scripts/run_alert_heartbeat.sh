#!/usr/bin/env bash
# Alerting-heartbeat runner — systemd entry point for the daily probe that
# is the seven-day-a-week floor under the sessions' own alert-channel
# watchdog (src/alert_watchdog.py).
#
# Same shape as run_daily_export.sh, run_pricing_refresh.sh and
# run_drift_check.sh, for the same reason: the script needs `.env` sourced
# or it has no credentials. Here that matters twice over — a heartbeat that
# ran without credentials would report the alert channel broken every single
# day and be measuring nothing but its own misconfiguration.
#
# Arguments are passed through (`--status` prints the record and sends
# nothing). The weekly digest this wrapper used to also drive is gone —
# a routine confirmation the operator learns to ignore is worth nothing,
# and a week of undetected breakage is not monitoring.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
# Linux: /usr/bin/timeout. macOS fallback: brew coreutils via TIMEOUT_OVERRIDE
# (same convention as run_if_et_window.sh and run_daily_export.sh).
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 60s is generous for at most three short HTTPS calls, each carrying the
# notifier's own 5s client timeout.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/alert_heartbeat.py "$@"
