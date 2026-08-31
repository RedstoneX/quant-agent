#!/usr/bin/env bash
# Deploy-drift check runner — systemd entry point.
#
# WHY THIS FILE EXISTS. Until 2026-08-31 the unit invoked the venv Python on
# scripts/check_deploy_drift.py directly, with no EnvironmentFile and no
# .env sourcing. The qamc systemd user environment carries zero TELEGRAM_*
# variables, so `TelegramNotifier` constructed itself disabled and every
# drift alert would have been printed to the journal and delivered to
# nobody. The check had only ever reported "in sync", so the one run that
# mattered had never happened — the alarm that exists to say a merge never
# reached production could not speak.
#
# Same shape as run_daily_export.sh and run_pricing_refresh.sh, for the same
# reason each of those gives: the script needs `.env` sourced or its alert
# path is credential-less, which turns a loud failure into a silent one.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is
# a read-only git comparison on a fixed-time timer, so none of its window /
# dedup / session-lock machinery applies.
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

# 60s is generous for one `git fetch` (the script's own cap is 20s) plus one
# Telegram push. Kept under the unit's TimeoutStartSec so the wrapper kills
# Python before systemd kills the wrapper.
exec "$TIMEOUT" --kill-after=15 60 "$PYTHON" scripts/check_deploy_drift.py "$@"
