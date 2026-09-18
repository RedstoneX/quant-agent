#!/usr/bin/env bash
# Per-item deployment check runner — the entry point a timer would use.
#
# Same shape as run_drift_check.sh, and for the same reason it gives: the
# script's alert path is `TelegramNotifier`, which reads TELEGRAM_* from the
# environment and constructs itself DISABLED when they are absent. The qamc
# systemd user environment carries none of them, so without `.env` sourced
# here every finding would be printed to the journal and delivered to
# nobody — a loud failure turned into a silent one.
#
# DELIBERATELY NOT CHAINED ONTO run_drift_check.sh. That wrapper `exec`s one
# script inside a 60s timeout sized for one `git fetch` plus one Telegram
# push. This check walks up to 200 commits of docs/WORK.md history (measured
# at 6.9s on a full clone, 2026-09-18) and folding it in would spend the
# existing deploy-drift alarm's headroom and couple its exit code to this
# one. An alarm that starts timing out is an alarm that gets muted.
#
# NOT YET ON A TIMER. There is no tracked systemd unit for this: installing
# one is a deploy action, and a unit tracked-but-not-installed is reported
# by check_unit_drift.py as `undeployed` and alerts until somebody installs
# it. Wiring it is one unit plus one timer and is an owner/ops call, not
# this change's to make.
#
# Arguments are passed through to the Python script.
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

exec "$TIMEOUT" --kill-after=15 120 "$PYTHON" \
    scripts/check_item_deployment.py "$@"
