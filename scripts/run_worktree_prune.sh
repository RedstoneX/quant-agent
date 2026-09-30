#!/usr/bin/env bash
# Worktree cleanup runner — systemd entry point.
#
# Calls scripts/prune_stale_worktrees.sh which:
# - Removes stale registrations whose directories no longer exist
# - Removes abandoned session-scratch worktrees (5+ days old, clean, merged)
#
# Conservative and safe: never touches /home/ubuntu/worktrees/ (active
# sessions) or other tenants. Only removes merged, clean scratch directories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# Linux: /usr/bin/timeout. macOS fallback: brew coreutils via TIMEOUT_OVERRIDE.
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 30s for the cleanup sweep: registry scan + file removal is not instant.
exec "$TIMEOUT" --kill-after=10 30 bash scripts/prune_stale_worktrees.sh
