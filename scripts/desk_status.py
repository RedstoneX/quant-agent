#!/usr/bin/env python3
"""On-demand desk status — one Telegram message, read-only, any time.

    scripts/desk_status.py --dry-run   # print the message, send nothing
    scripts/desk_status.py             # send it to the owner's channel

WHAT IT IS
    "Tell me where the desk stands, now." It reports state; it does not
    produce a view. There is no analysis in it and no opinion in it.

WHAT IT NEVER DOES
    It places no order, cancels no order and touches no position. It makes
    no model call of any kind, so it cannot move the day's AI spend or trip
    the cost circuit. It writes nothing to the database — every read goes
    through a read-only SQLite connection or a broker GET. It takes no
    session lock and sets no once-per-day stamp, so a scheduled session
    running at the same moment is completely unaffected by it.

WHERE THE FORMAT COMES FROM
    The message is rendered by `src.trader_feed.format_desk_status`, which
    is a thin assembler over `_format_hourly_desk_check` — the same
    function that builds the scheduled top-of-hour DESK CHECK message.
    This script holds no copy of the wording, so when that formatter
    changes, this command changes with it and cannot drift from what the
    scheduled messages look like.

HONESTY
    Two fields an on-demand read genuinely cannot know — the stop-coverage
    audit and the movers scan, both of which only run inside a scheduled
    session — are rendered "not available" with the reason. They are never
    rendered as "OK", as zero, or as blank. If the broker cannot be read at
    all, the command refuses to send anything rather than publish a message
    with an invented P&L.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("desk_status")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send (or print) the current desk status to Telegram.",
    )
    parser.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to the config file (default: config/settings.yaml).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the message to stdout and send nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from src.config import load_config
    from src.execution.broker import AlpacaBroker
    from src.trader_feed import format_desk_status

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        return 2

    config = load_config(config_path)

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        print(
            "ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set — cannot read "
            "the book, so there is no honest status to send.",
            file=sys.stderr,
        )
        return 2

    start = time.monotonic()
    # No kill-switch path is threaded through on purpose: this object is
    # only ever asked for `get_account` / `get_positions`, both read-only
    # GETs, and the kill switch exists to block ORDER submission.
    broker = AlpacaBroker(api_key, secret_key, paper=bool(config.alpaca.paper))
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001
        # Refuse rather than send a message whose P&L would be made up.
        print(f"ERROR: broker read failed, nothing sent: {exc}", file=sys.stderr)
        return 3

    message = format_desk_status(account, positions, time.monotonic() - start)

    if args.dry_run:
        print(message)
        return 0

    from src.notifier import TelegramNotifier

    notifier = TelegramNotifier()
    notifier.mission_control_url = config.notifications.mission_control_url
    # preserve_structural_markup=True: the shared trader-feed formatters
    # embed literal <b>/<blockquote expandable> tags on purpose.
    sent = notifier.send(message, preserve_structural_markup=True)
    if not sent:
        print(
            "ERROR: Telegram send failed or the notifier is not configured "
            "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
