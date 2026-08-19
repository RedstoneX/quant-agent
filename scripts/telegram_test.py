#!/usr/bin/env python3
"""Non-trading Telegram delivery check.

Answers one operator question — "will a scheduled session's status push
actually arrive?" — without running a trading session. It touches no
broker, no LLM, no market data and no trading DB: it imports only
`src.notifier` and posts one plain sendMessage.

Usage (from the project root, as the runtime account):

    .venv/bin/python scripts/telegram_test.py            # report + send
    .venv/bin/python scripts/telegram_test.py --dry-run  # report only

Exit 0 means the push was delivered — or, under --dry-run, that the
credentials a scheduled session would use are present and un-muted.
Exit 1 means a push would not arrive (missing creds, muted, or the API
rejected us) and the printed reason says which.

Secret discipline: this prints only SET / NOT SET, never a value, and the
notifier redacts the bot token out of its failure log line — so the
credential values this tool handles are safe to paste into a support
conversation.

Known limit: a green result here does not prove the *wrapper* can start a
session. Both wrappers run under `set -e` and `source` the env file, so a
bash syntax error anywhere in it aborts them before main.py ever runs,
while the loader below simply skips the bad line and still reports
DELIVERED. That ".env was never sourced" class is what the external
dead-man's switch (HEALTHCHECKS_URL) exists to catch — see main.py's
notifier construction comment.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TELEGRAM_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_DISABLED")

_ENV_FILENAME = ".env"


def _load_env_file() -> tuple[bool, set[str]]:
    """Apply the TELEGRAM_* vars from the project env file.

    Returns (file_present, names_taken_from_file).

    Two deliberate differences from
    `scripts/export_alpaca_trades.py::_load_env_file`:

    - **The file wins over the inherited environment.** That loader is
      env-wins; the wrappers this script has to predict are not. Both
      `run_if_et_window.sh` and `run_daily_export.sh` do
      `set -a; source .env; set +a`, so a scheduled session sees the
      file's value even when a stale one is exported in some shell.
      Mirroring that is the entire point — an env-wins diagnostic would
      report DELIVERED off a shell token while every scheduled session
      kept failing on the file's stale one.
    - **Only TELEGRAM_* is applied.** A delivery probe has no business
      pulling broker or LLM credentials into its process.

    Everything else matches: line-based `KEY=value`, `#` comments, and
    the `export KEY=value` form the production file uses because bash
    sources it.

    Not a full bash parser: an inline `KEY=value  # note` keeps the
    comment here where bash would strip it. That direction is a false
    RED — the tool reports a send failure on a file real sessions handle
    — so it fails safe and is not worth a shell-grade parser.
    """
    env_path = PROJECT_ROOT / _ENV_FILENAME
    if not env_path.exists():
        return False, set()
    applied: set[str] = set()
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in _TELEGRAM_VARS:
                continue
            os.environ[k] = v.strip().strip('"').strip("'")
            applied.add(k)
    except Exception as exc:  # noqa: BLE001
        print(f"{_ENV_FILENAME} present but unreadable "
              f"({exc.__class__.__name__}); using the process environment only")
        return False, applied
    return True, applied


def _report_config(file_present: bool, from_file: set[str]) -> None:
    print(f"project root       : {PROJECT_ROOT}")
    print(f"{_ENV_FILENAME:<19}: {'read' if file_present else 'not read'}")
    for name in _TELEGRAM_VARS:
        if not os.getenv(name, "").strip():
            state = "NOT SET"
        else:
            # Naming the source is what turns "NOT SET, but I definitely
            # set it" into a five-second diagnosis.
            state = f"SET (from {_ENV_FILENAME if name in from_file else 'environment'})"
        print(f"{name:<19}: {state}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one non-trading Telegram test message.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report configuration state only; send nothing.",
    )
    args = parser.parse_args()

    file_present, from_file = _load_env_file()

    # Imported after the env-file load so the notifier reads the same
    # environment a wrapper-launched session would.
    from src.notifier import TelegramNotifier

    notifier = TelegramNotifier()
    _report_config(file_present, from_file)
    print(f"notifier enabled   : {notifier.enabled}")

    if not notifier.enabled:
        # Ask the notifier what it actually parsed rather than
        # re-deriving the kill-switch predicate here. It accepts exactly
        # 1/true/yes, so `TELEGRAM_DISABLED=0` leaves pushes ON — a
        # re-derived `if os.getenv(...)` truthiness check would report
        # MUTED for a value the notifier ignores, and send the operator
        # chasing the wrong variable.
        if notifier.token and notifier.chat_id:
            print("\nRESULT: MUTED — credentials are present but "
                  "TELEGRAM_DISABLED is set to a value the notifier honours "
                  "(1/true/yes). Remove the line to restore pushes.")
        else:
            print("\nRESULT: NOT CONFIGURED — set TELEGRAM_BOT_TOKEN and "
                  "TELEGRAM_CHAT_ID (see .env.example). Trading is unaffected "
                  "either way; status pushes are simply silent.")
        return 1

    if args.dry_run:
        print("\nRESULT: CONFIGURED — --dry-run, nothing sent.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        "🧪 quant-agent Telegram test\n"
        f"{stamp}\n"
        "Non-trading check — no orders, no LLM calls, no market data. "
        "If you can read this, scheduled session status pushes will arrive."
    )
    if notifier.send(text):
        print("\nRESULT: DELIVERED — check the chat.")
        return 0

    # send() swallows the exception by design (trading must never fail
    # because Telegram is down), so the cause is in the token-redacted
    # 'Telegram notify failed' warning it logged, not in a return value
    # we can inspect here.
    print("\nRESULT: SEND FAILED — see the logged 'Telegram notify failed' "
          "warning above for the cause (bad token, wrong chat_id, or "
          "network/egress blocked).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
