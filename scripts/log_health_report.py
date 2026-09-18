#!/usr/bin/env python3
"""Read the desk's log for the window since the last report and push a plain-
English health verdict to Telegram.

Read-only against everything that matters: it opens log files, it reads
`docs/WORK.md` to find out what is already tracked, and the only thing it
writes is its own watermark. It never touches the broker, never calls a model
and never blocks trading — it is deliberately safe to fire while a session is
running.

Usage:
  python scripts/log_health_report.py                 # normal scheduled run
  python scripts/log_health_report.py --dry-run       # print, send nothing,
                                                      # leave the watermark alone
  python scripts/log_health_report.py --since-hours 48 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import log_health  # noqa: E402
from src.notifier import TelegramNotifier  # noqa: E402

logger = logging.getLogger("log_health_report")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="directory holding quant_agent.log and its rotations",
    )
    parser.add_argument("--state", type=Path, default=None, help="watermark file")
    parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="ignore the watermark and look back this many hours (diagnostic)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message; send nothing and do not advance the watermark",
    )
    parser.add_argument(
        "--no-telegram", action="store_true", help="analyse and print, never send"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    now = datetime.now(tz=log_health.LOG_TZ).replace(microsecond=0)
    if args.since_hours is not None:
        start = now - timedelta(hours=args.since_hours)
        records = log_health.read_window(start, now, args.log_dir)
        state = log_health.load_state(args.state)
        report = log_health.analyse(
            records, start, now, log_dir=args.log_dir, previous=state.get("reported")
        )
    else:
        report = log_health.build_report(
            now=now, log_dir=args.log_dir, state_path=args.state
        )

    messages = log_health.render(report)
    for message in messages:
        print(message)
        print("-" * 40)

    if args.dry_run or args.no_telegram:
        return 0

    notifier = TelegramNotifier()
    delivered = True
    for message in messages:
        # `preserve_structural_markup=True` keeps the bold title line; the
        # message is built from fixed sentences and integers, so there is no
        # model or broker text in it that could carry stray markup.
        delivered = notifier.send(message, preserve_structural_markup=True) and delivered

    if not delivered:
        # The watermark is NOT advanced on a failed send. A report the owner
        # never received must not be treated as delivered, or the window it
        # covered would be skipped for good — the silent-gap failure this whole
        # job exists to catch.
        logger.error("log-health: report could not be delivered; watermark unchanged")
        return 1

    log_health.save_state(report, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
