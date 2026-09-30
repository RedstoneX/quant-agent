#!/usr/bin/env bash
# Stored-target check runner — systemd entry point.
#
# Same shape as run_board_hygiene_check.sh, run_unit_drift_check.sh and
# run_drift_check.sh, for the same recorded reason each of those gives: the
# script raises a Telegram alert, and the qamc systemd user environment
# carries zero TELEGRAM_* variables. Without `.env` sourced,
# `TelegramNotifier` disables itself and a finding would be printed to the
# journal and delivered to nobody — which for this check means a position
# quoting a target with a wall in front of it stays exactly as invisible as
# it was before the check existed.
#
# `.env` is also what carries the market-data credentials. Unlike the other
# three checks this one READS BARS for every held symbol, so an unwrapped
# run would not merely fail to speak, it would fail to measure.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is
# a read-only check on a fixed-time timer, so none of its window / dedup /
# session-lock machinery applies.
#
# Arguments are passed through to the Python script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python"
# Linux: /usr/bin/timeout. macOS fallback: brew coreutils via TIMEOUT_OVERRIDE
# (same convention as run_if_et_window.sh and run_board_hygiene_check.sh).
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 600s, an order of magnitude above the other read-only checks, because this
# one pulls a full bar history per held position from the market-data
# provider rather than reading local files. Measured 2026-09-30 against the
# live book: 11 held positions, 6.3s wall clock warm. The headroom is for a
# cold cache and a slow provider day, and the kill-after is what stops a
# hung fetch holding the timer open.
exec "$TIMEOUT" --kill-after=30 600 "$PYTHON" scripts/check_stored_targets.py "$@"
