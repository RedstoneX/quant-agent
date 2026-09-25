#!/usr/bin/env bash
# Stale-worktree prune runner — systemd entry point.
#
# Wraps scripts/prune_worktrees.py --prune so a scheduled timer clears the
# session-scratch worktrees finished sessions leave registered (board item
# 139). Unlike the drift/hygiene runners this needs NO .env: the sweep sends
# no Telegram and builds no notifier — it is a pure local `git worktree`
# operation. Kept as a wrapper anyway so the systemd unit has a stable entry
# point and a timeout, matching run_drift_check.sh / run_board_hygiene_check.sh.
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

# 120s is generous: listing worktrees plus a `git worktree remove` per stale
# entry, all local. Default args prune; a manual invocation can override.
if [[ "$#" -eq 0 ]]; then
    set -- --prune
fi
exec "$TIMEOUT" --kill-after=15 120 "$PYTHON" scripts/prune_worktrees.py "$@"
