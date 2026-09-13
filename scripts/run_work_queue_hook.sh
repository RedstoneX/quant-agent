#!/usr/bin/env bash
# Stop-hook wrapper for scripts/work_queue.py.
#
# A shell wrapper rather than a direct command in a settings file for one
# reason: this must NEVER be the thing that breaks a session. A missing
# interpreter, a missing checkout, an import error — every one of those exits
# 0 here and lets the session stop. The hook exists to catch a dropped item,
# not to hold anyone hostage to its own health.
#
# SAFE TO INSTALL GLOBALLY. It is written to be wired at USER level, in
# ~/.claude/settings.json, because a project-scoped hook only loads for
# sessions started inside the project folder — which is why this one had
# never fired once between being written and 2026-09-13. Wired globally it
# runs at the end of every session on this machine, so the FIRST thing it
# does is establish that the current directory is a checkout of THIS
# repository and exit 0, silently, when it is not. Another project must
# never see a line of output from it.
#
# Exit 0  the session may stop (also: not this repository, or this hook is
#         broken — every uncertainty resolves here)
# Exit 2  blocked; the reason is on stderr. 2 is the only code the harness
#         treats as blocking a Stop hook: 0 ends the conversation and every
#         OTHER non-zero code is reported as a hook error and ends it too.
#         This wrapper used to pass 1 through, which blocked nothing.
set -uo pipefail

# --- is this our repository? ------------------------------------------------
# The session's own checkout, not this script's, so a worktree checks itself.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$ROOT" ] || exit 0
# Two files no other repository on this machine has together. Cheaper and
# more honest than matching a path, which breaks on every new worktree.
[ -f "$ROOT/scripts/work_queue.py" ] || exit 0
[ -f "$ROOT/docs/WORK.md" ] || exit 0

SCRIPT="$ROOT/scripts/work_queue.py"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || exit 0

"$PY" "$SCRIPT" --stop-hook
status=$?
# Only 2 means "do not stop". Anything else — a crash, a bad import, a
# timeout kill — is this hook's problem, not the session's.
[ "$status" -eq 2 ] && exit 2
exit 0
