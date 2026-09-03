#!/usr/bin/env bash
# Silence-watchdog runner — systemd entry point for
# quant-agent-silence-heartbeat.timer.
#
# Same shape as run_alert_heartbeat.sh, run_pricing_refresh.sh and
# run_drift_check.sh: needs `.env` sourced for TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID, since the alert this check sends (when it has anything
# to say) goes through the same TelegramNotifier every other alarm uses.
#
# Sends nothing on a healthy desk — see scripts/silence_heartbeat.py and
# src/silence_watchdog.py for what "healthy" means here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 60s is generous: one read-only SQLite query plus, at most, one Telegram
# send — no LLM call, no broker call.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/silence_heartbeat.py "$@"
