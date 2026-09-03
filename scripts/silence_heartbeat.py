#!/usr/bin/env python3
"""Desk-wide silence watchdog — alerts when NOTHING has run, not when
something ran and failed.

WHAT THIS ADDS OVER `scripts/alert_heartbeat.py`
-------------------------------------------------
That script (and the sessions themselves, via `src/alert_watchdog.py`) prove
the Telegram PIPE still works. Neither one asks whether the desk has done
any WORK. A latched circuit, a database that cannot be opened, a crash on
every startup — any total failure, including ones nobody has imagined yet —
looks identical from here: no completed session gets recorded, in any mode,
for longer than a full scheduled day should ever allow. See
`src/silence_watchdog.py` for the full design and why `alert_channel_checks`
(not `agent_logs`) is the source of truth for "a session happened".

This script does the recording AND the deciding in one call: read what the
database currently shows, fold it into the on-box marker, and alert through
the one proven Telegram path (`src.notifier.send_owner_alert`) if the
desk-wide silence has crossed the (currently placeholder) threshold. It
sends nothing when nothing is wrong — the entire noise budget for a healthy
desk is zero messages, matching `alert_heartbeat.py` and
`alert_watchdog.session_note`'s own rule.

USAGE
    python scripts/silence_heartbeat.py                 # check, alert if needed, exit 0/1
    python scripts/silence_heartbeat.py --status         # print the record, send nothing
    python scripts/silence_heartbeat.py --threshold 8    # override the placeholder for one run

EXIT CODES
    0  not silent, or silent but already alerted for this baseline
    1  silent AND a new alert was (attempted to be) sent this run
    2  bad arguments
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.silence_watchdog import (  # noqa: E402
    DEFAULT_SILENT_WINDOW_THRESHOLD,
    STATE_PATH,
    alert_text,
    check_silence,
    load_state,
)


def run_check(threshold: int) -> tuple[int, str]:
    status = check_silence(threshold=threshold)

    if not status.is_silent:
        return 0, (
            f"silence_heartbeat: OK — {status.consecutive_silent_windows}/"
            f"{status.threshold} consecutive silent windows; last known "
            f"session {status.last_known_session_at or 'never'}"
        )

    if status.already_alerted_for_this_baseline:
        return 0, (
            f"silence_heartbeat: STILL SILENT ({status.consecutive_silent_windows}/"
            f"{status.threshold} windows) but already alerted for this baseline "
            f"({status.last_known_session_at or 'never'}); not re-sending"
        )

    from src.notifier import send_owner_alert

    text = alert_text(status)
    print(text, file=sys.stderr)
    delivered = bool(send_owner_alert(text))
    return 1, (
        f"silence_heartbeat: SILENT — {status.consecutive_silent_windows}/"
        f"{status.threshold} consecutive windows with no completed session; "
        f"alert {'delivered' if delivered else 'could NOT be delivered'}"
    )


def run_status() -> tuple[int, str]:
    state = load_state()
    lines = [
        f"on-box record: {STATE_PATH}",
        f"  last known session: {state.get('last_known_session_at') or 'never'}",
        f"  alerted for baseline: {state.get('alerted_for_baseline') or 'no'}",
        f"  updated at: {state.get('updated_at') or 'never'}",
        f"  default threshold (placeholder, pending owner confirmation): "
        f"{DEFAULT_SILENT_WINDOW_THRESHOLD} scheduled windows",
    ]
    return 0, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Alert when the desk has recorded no completed session "
        "in N consecutive scheduled windows, across all modes.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="print the record and exit; sends nothing, checks nothing fresh",
    )
    parser.add_argument(
        "--threshold", type=int, default=DEFAULT_SILENT_WINDOW_THRESHOLD,
        help=(
            "consecutive scheduled windows with no completed session before "
            f"alerting (default {DEFAULT_SILENT_WINDOW_THRESHOLD} — a "
            "placeholder pending owner confirmation, see docs/WORK.md item 17c)"
        ),
    )
    args = parser.parse_args(argv)

    if args.threshold < 1:
        print("--threshold must be at least 1", file=sys.stderr)
        return 2

    if args.status:
        code, line = run_status()
    else:
        code, line = run_check(args.threshold)

    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
