#!/usr/bin/env bash
# Board-hygiene check runner — systemd entry point.
#
# Same shape as run_unit_drift_check.sh and run_drift_check.sh, for the
# same reason each of those gives: the script raises a Telegram alert, and
# the qamc systemd user environment carries zero TELEGRAM_* variables.
# Without `.env` sourced, `TelegramNotifier` disables itself and a finding
# would be printed to the journal and delivered to nobody.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is
# a read-only filesystem read on a fixed-time timer, so none of its
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

# 60s is generous for reading two small files plus one Telegram push. The
# check itself is pure local filesystem work — no network, no git.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/check_board_hygiene.py "$@"
