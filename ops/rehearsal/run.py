"""Command-line entry point for a full-session rehearsal.

    python ops/rehearsal/run.py --help

See ops/rehearsal/README.md for the runbook, and runner.py for what is real
and what is not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _bootstrap() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rehearsal",
        description=(
            "Run a complete trading session end to end against real "
            "components, offline, for free, and report in plain language "
            "what would have happened."
        ),
    )
    parser.add_argument(
        "--session", default="morning",
        choices=["morning", "midday", "close", "evening", "intra_check"],
    )
    parser.add_argument(
        "--source-db", required=True,
        help="production SQLite database to snapshot (never written to)",
    )
    parser.add_argument(
        "--source-data", default=None,
        help="production data directory to copy caches from",
    )
    parser.add_argument(
        "--sudo-user", default=None,
        help=(
            "POSIX account that owns the production files; the snapshot and "
            "copy run as this user via 'sudo -n -u'"
        ),
    )
    parser.add_argument(
        "--replay-run", default=None,
        help=(
            "agent_logs run_id whose recorded model responses this rehearsal "
            "replays; omit to draw on all recorded history"
        ),
    )
    parser.add_argument(
        "--as-of", default=None,
        help="ET date/time to freeze the session clock at (YYYY-MM-DD[THH:MM])",
    )
    parser.add_argument(
        "--sandbox", default=None,
        help="scratch directory to build in (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep-sandbox", action="store_true",
        help="do not delete the scratch directory afterwards",
    )
    parser.add_argument(
        "--fill-model", default="immediate", choices=["immediate", "unfilled"],
        help=(
            "how submitted orders are modelled: 'immediate' fills everything "
            "at the price asked, 'unfilled' fills nothing"
        ),
    )
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
        help=(
            "override a config value, dotted "
            "(e.g. llm_cost_circuit.session_reserved_exposure_limit_usd=1.80)"
        ),
    )
    parser.add_argument(
        "--pricing-cache-age-hours", default=None, metavar="HOURS|inherit",
        help=(
            "age to stamp on the sandbox's OpenRouter pricing cache "
            "(default: fresh). Pass a value past 24h + the configured grace "
            "to rehearse the fail-closed staleness path on purpose, or "
            "'inherit' to use production's real mtime"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON as well")
    return parser


def _parse_pricing_age(text: str | None):
    """Resolve --pricing-cache-age-hours into the runner's argument."""
    from ops.rehearsal.runner import DEFAULT_PRICING_CACHE_AGE_HOURS

    if text is None:
        return DEFAULT_PRICING_CACHE_AGE_HOURS
    if text.strip().lower() == "inherit":
        return None
    try:
        value = float(text)
    except ValueError:
        raise SystemExit(
            f"--pricing-cache-age-hours expects a number of hours or "
            f"'inherit', got {text!r}"
        )
    if value < 0:
        raise SystemExit("--pricing-cache-age-hours must not be negative")
    return value


def _parse_overrides(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set expects KEY=VALUE, got {pair!r}")
        key, _, raw = pair.partition("=")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        out[key.strip()] = value
    return out


def _parse_as_of(text: str | None):
    from src.trading_calendar import ET

    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            parsed = parsed.replace(hour=9, minute=35)
        return parsed.replace(tzinfo=ET)
    raise SystemExit(f"could not read --as-of {text!r}; use YYYY-MM-DD[THH:MM]")


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    args = build_parser().parse_args(argv)

    from ops.rehearsal.isolation import Sandbox
    from ops.rehearsal.runner import run_rehearsal

    sandbox_root = Path(args.sandbox) if args.sandbox else Path(
        tempfile.mkdtemp(prefix="qamc-rehearsal-")
    )
    try:
        sandbox = Sandbox.prepare(
            source_db=args.source_db,
            root=sandbox_root,
            source_data_dir=args.source_data,
            sudo_user=args.sudo_user,
        )
        report = run_rehearsal(
            sandbox,
            session=args.session,
            now_et=_parse_as_of(args.as_of),
            replay_run=args.replay_run,
            config_overrides=_parse_overrides(args.overrides),
            fill_model=args.fill_model,
            production_db=args.source_db,
            sudo_user=args.sudo_user,
            pricing_cache_age_hours=_parse_pricing_age(
                args.pricing_cache_age_hours
            ),
        )
        print(report.render())
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0 if report.verdict == "PASS" else 1
    finally:
        if not args.keep_sandbox and not args.sandbox:
            shutil.rmtree(sandbox_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
