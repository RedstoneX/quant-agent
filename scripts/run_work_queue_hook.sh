#!/usr/bin/env bash
# Stop-hook wrapper for scripts/work_queue.py.
#
# A shell wrapper rather than a direct command in .claude/settings.json for
# one reason: this must NEVER be the thing that breaks a session. A missing
# interpreter, a missing checkout, an import error — every one of those exits
# 0 here and lets the session stop. The hook exists to catch a dropped item,
# not to hold anyone hostage to its own health.
#
# Exit 0  the session may stop
# Exit 1  work_queue.py is handing an item back; its reason is on stderr
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPT="$ROOT/scripts/work_queue.py"
[ -f "$SCRIPT" ] || exit 0

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || exit 0

"$PY" "$SCRIPT" --stop-hook
status=$?
# Only 1 means "there is work". Anything else — a crash, a bad import, a
# timeout kill — is this hook's problem, not the session's.
[ "$status" -eq 1 ] && exit 1
exit 0
