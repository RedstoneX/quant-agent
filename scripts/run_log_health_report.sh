#!/usr/bin/env bash
# Log-health report runner — systemd entry point.
#
# Same shape as run_unit_drift_check.sh / run_drift_check.sh, and for the same
# recorded reason: the script raises a Telegram message, and the qamc systemd
# user environment carries zero TELEGRAM_* variables. Without `.env` sourced,
# `TelegramNotifier` disables itself and the report is printed to the journal
# and delivered to nobody.
#
# NOT routed through run_if_et_window.sh. That wrapper exists for the six
# ET-windowed TRADING sessions: it rejects unknown modes by design, and its
# once-per-session marker, cross-mode session lock and 20-minute trading
# timeout all apply to a Python process that talks to the broker. This one
# reads files. The ET awareness it does need is the firing time, and the timer
# gets that from systemd's own IANA zone support (`America/New_York`), which is
# how quant-agent-drift-check and quant-agent-unit-drift already do it.
#
# Arguments are passed through.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON_OVERRIDE:-${PROJECT_ROOT}/.venv/bin/python}"
TIMEOUT="${TIMEOUT_OVERRIDE:-/usr/bin/timeout}"

cd "$PROJECT_ROOT"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

# 180s: the duration-measuring families read the rotated logs (about 50MB of
# retained history), which is the slowest thing this job does. No network
# beyond one Telegram push, no model call, no broker call.
#
# NOT `exec` any more (it was, until the auto-fix handoff below was added).
# `exec` replaces this shell, so nothing can run after the report — and the
# whole point of the handoff is that it happens AFTER the report has actually
# finished, not at a guessed time.
set +e
"$TIMEOUT" --kill-after=15 180 "$PYTHON" scripts/log_health_report.py "$@"
REPORT_STATUS=$?
set -e

# --- auto-fix handoff -------------------------------------------------------
# The last step of the report is to say that the report is over.
#
# WHY A MARKER AND NOT A DIRECT CALL. The owner's instruction is that the
# auto-fix session must fire off the real completion of this script, never off
# a clock offset guessed from how long a report usually takes — a slow report
# would otherwise be read while it was still being written. Calling the
# auto-fix runner from this line directly is impossible, not merely awkward:
# this script runs as `qamc`, the trading service account, and the auto-fix
# session must run as `ubuntu`, the engineering account that holds the git
# checkout and the CLI credential. `qamc` has no sudo on this box at all
# [verified 2026-09-20: "User qamc is not allowed to run sudo"], and giving it
# one so it could launch code as `ubuntu` would hand the account that trades
# the ability to execute as the account that can rewrite the code that trades.
# That is a credential-boundary redesign and is owner-only.
#
# So this writes a marker and `quant-agent-auto-fix.path`, an inotify unit in
# ubuntu's own systemd instance, starts the session the instant the marker
# changes. Same pattern the desk already uses in
# `scripts/systemd/quant-agent-status-board.path`. Event-driven, no clock, no
# delay, and the trigger cannot fire before this line is reached.
#
# BEST EFFORT, ALWAYS. Success or failure of the report, the marker is written
# — a report that failed is exactly the kind the auto-fix session should read.
# Every failure mode here is swallowed: this handoff must never be the reason
# the health report is recorded as broken. If the directory does not exist
# (nothing installed the handoff yet, which is the shipped state), that is a
# silent no-op and the report behaves exactly as it did before.
AUTO_FIX_HANDOFF_DIR="${AUTO_FIX_HANDOFF_DIR:-/var/lib/quant-agent/handoff}"
if [[ -d "$AUTO_FIX_HANDOFF_DIR" && -w "$AUTO_FIX_HANDOFF_DIR" ]]; then
    {
        printf 'report_finished_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'report_finished_et=%s\n' "$(TZ=America/New_York date +%Y-%m-%dT%H:%M:%S)"
        printf 'report_exit_status=%s\n' "$REPORT_STATUS"
    } > "${AUTO_FIX_HANDOFF_DIR}/health-report-complete.tmp" 2>/dev/null \
        && mv -f "${AUTO_FIX_HANDOFF_DIR}/health-report-complete.tmp" \
                 "${AUTO_FIX_HANDOFF_DIR}/health-report-complete" 2>/dev/null \
        || true
fi

# The report's own exit status is what systemd sees, unchanged. Exit 1 still
# means the report could not be DELIVERED, which is still a red unit.
exit "$REPORT_STATUS"
