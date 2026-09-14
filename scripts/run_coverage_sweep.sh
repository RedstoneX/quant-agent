#!/usr/bin/env bash
# Stop-coverage sweep runner — systemd entry point for
# quant-agent-coverage-sweep.timer.
#
# WHY THIS EXISTS SEPARATELY FROM run_alert_heartbeat.sh
# -------------------------------------------------------
# The coverage check rides on the 06:15 ET alert-heartbeat because that unit
# is the one thing still running while the trading timers are stopped. But
# 06:15 is more than three hours before the bell, and the thing the check
# now has to DO — put back the DAY stop over a fractional remainder — cannot
# be done into a shut market. So the same check also needs a run that lands
# INSIDE a session, and that run must not fire the Telegram channel probe
# every thirty minutes.
#
# `--coverage-only` is that run. It self-gates on the exchange calendar the
# broker publishes (is_trading_day / get_session_open / get_session_close),
# so a tick outside a real session reads the broker, finds it shut, and
# places nothing.
#
# Same shape as run_silence_heartbeat.sh and run_alert_heartbeat.sh: `.env`
# is sourced for the broker credentials and for TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID, since a placement that fails alerts through the same
# TelegramNotifier every other alarm uses.
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

# 120s: a handful of broker reads, and at most one stop placement whose own
# retry burst is bounded at ~2 seconds. No LLM call.
exec "$TIMEOUT" --kill-after=15 120 "$PYTHON" scripts/alert_heartbeat.py \
    --coverage-only "$@"
