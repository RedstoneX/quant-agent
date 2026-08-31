#!/usr/bin/env bash
# systemd unit-drift check runner — systemd entry point.
#
# Same shape as run_drift_check.sh, run_daily_export.sh,
# run_pricing_refresh.sh and run_alert_heartbeat.sh, for the same reason
# each of those gives: the script raises a Telegram alert, and the qamc
# systemd user environment carries zero TELEGRAM_* variables. Without `.env`
# sourced, `TelegramNotifier` disables itself and the alert is printed to
# the journal and delivered to nobody — the exact defect PR #175 fixed in
# the deploy-drift unit, which is not worth reproducing in its sibling.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is
# a read-only filesystem comparison on a fixed-time timer, so none of its
# window / dedup / session-lock machinery applies.
#
# Arguments are passed through to the Python script.
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

# 60s is generous for reading two small directories plus one Telegram push.
# The comparison itself is pure local filesystem work — no network, no git.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/check_unit_drift.py "$@"
