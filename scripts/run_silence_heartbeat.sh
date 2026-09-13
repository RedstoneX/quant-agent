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

# A DELIBERATELY PAUSED DESK IS NOT A SILENT ONE.
#
# This watchdog fires on the absence of sessions. When the owner pauses the
# desk, absence of sessions is the intended state, and an alarm that pages
# every 30 minutes about a thing somebody chose on purpose is an alarm that
# gets muted — and a muted alarm is why item 17c exists in the first place.
#
# The pause is expressed by disabling the six mode timers, so that is what
# is read here rather than a flag somebody has to remember to set. Kept in
# the wrapper, not in `src/silence_watchdog.py`: whether systemd units are
# enabled is a deployment fact, and the watchdog stays a pure reader of the
# database. `--paused-ok` bypasses this for the tests and for a manual run.
paused_ok=0
args=()
for arg in "$@"; do
    if [[ "$arg" == "--paused-ok" ]]; then
        paused_ok=1
    else
        args+=("$arg")
    fi
done

if [[ "$paused_ok" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
    enabled_modes=0
    for unit in morning midday intra_check close evening daily; do
        if systemctl --user is-enabled "quant-agent-${unit}.timer" \
                >/dev/null 2>&1; then
            enabled_modes=$((enabled_modes + 1))
        fi
    done
    if [[ "$enabled_modes" -eq 0 ]]; then
        echo "every trading-mode timer is disabled: the desk is paused on" \
             "purpose, so its silence is not a finding. Nothing checked."
        exit 0
    fi
fi

# 60s is generous: one read-only SQLite query plus, at most, one Telegram
# send — no LLM call, no broker call.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/silence_heartbeat.py \
    ${args[@]+"${args[@]}"}
