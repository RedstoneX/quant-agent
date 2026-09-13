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
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
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
# CORRECTED 2026-09-13, same day it first shipped, after running it against
# the real box. Two things were wrong and both mattered:
#
#   * It asked systemd `is-enabled`. The pause on this desk was done by
#     STOPPING the timers, not disabling them — all six still read
#     "enabled" while none of them will ever fire. `is-active` is the
#     question that matches how the pause is actually expressed.
#   * The list of modes was typed by hand and was wrong: it named `daily`,
#     which is a maintenance timer the watchdog does not count as a
#     session, and omitted `earnings_preprocess`, which it does. `daily`
#     being active is precisely why the guard failed to trip. The modes are
#     now read from `src.trading_calendar.SESSION_WINDOWS`, the same source
#     the watchdog itself counts windows from, so the two cannot drift.
#
# Kept in the wrapper, not in `src/silence_watchdog.py`: whether systemd
# units are running is a deployment fact, and the watchdog stays a pure
# reader of the database. `--paused-ok` bypasses this for a manual run.
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
    # The modes come from the same place the watchdog counts windows from.
    # A failure to read them is NOT treated as "paused" — an unreadable
    # source must never silence an alarm, so the check goes ahead.
    modes="$("$PYTHON" -c \
        'from src.trading_calendar import SESSION_WINDOWS; print(" ".join(SESSION_WINDOWS))' \
        2>/dev/null || true)"
    if [[ -n "$modes" ]]; then
        live_modes=0
        for unit in $modes; do
            if systemctl --user is-active "quant-agent-${unit}.timer" \
                    >/dev/null 2>&1; then
                live_modes=$((live_modes + 1))
            fi
        done
        if [[ "$live_modes" -eq 0 ]]; then
            echo "no trading-mode timer is running: the desk is paused on" \
                 "purpose, so its silence is not a finding. Nothing checked."
            exit 0
        fi
    fi
fi

# 60s is generous: one read-only SQLite query plus, at most, one Telegram
# send — no LLM call, no broker call.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/silence_heartbeat.py \
    ${args[@]+"${args[@]}"}
