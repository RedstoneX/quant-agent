#!/usr/bin/env bash
# Worktree prune runner — systemd entry point.
#
# Removes stale git-worktree registrations whose directories no longer
# exist on disk (typical after session /tmp directories are cleaned up).
#
# Same shape as run_board_hygiene_check.sh: sourcing the environment
# ensures any needed env vars are available (though this command does not
# require them — it is defensive).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
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

# 5s is generous for a pure filesystem registry scan; prune is instant.
exec "$TIMEOUT" --kill-after=5 5 git worktree prune --verbose
