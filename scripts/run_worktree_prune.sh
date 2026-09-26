#!/usr/bin/env bash
# Stale-worktree prune runner — systemd entry point.
#
# Wraps scripts/prune_worktrees.py --prune so a scheduled timer clears the
# session-scratch worktrees finished sessions leave registered (board item
# 139). The sweep itself sends no Telegram, but this wrapper is still the
# unit's entry point, and test_alert_heartbeat.py requires every such
# wrapper to source `.env` regardless of whether it alerts today — a later
# edit to prune_worktrees.py that adds an alert must not silently lose its
# credentials. Kept as a wrapper anyway so the systemd unit has a stable
# entry point and a timeout, matching run_drift_check.sh /
# run_board_hygiene_check.sh.
#
# NOT routed through run_if_et_window.sh: that wrapper is for the six
# ET-windowed trading sessions and rejects unknown modes by design. This is a
# fixed-time git housekeeping sweep, so none of its window / dedup /
# session-lock machinery applies.
#
# Arguments are passed through to the Python script (e.g. drop --prune to make
# a manual run report-only, or add --min-age-days N).
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

# 120s is generous: listing worktrees plus a `git worktree remove` per stale
# entry, all local. Default args prune; a manual invocation can override.
if [[ "$#" -eq 0 ]]; then
    set -- --prune
fi
exec "$TIMEOUT" --kill-after=15 120 "$PYTHON" scripts/prune_worktrees.py "$@"
