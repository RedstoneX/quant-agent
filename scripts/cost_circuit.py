#!/usr/bin/env python3
"""Inspect, enforce, or manually reset QAMC's paid-analysis circuit.

This utility never calls a model. ``check`` is suitable for deployment: it
seeds existing same-day spend, latches if a limit is already reached, and
sends the one-time Telegram hold or shutdown alert. Expected session/day quota
holds cannot be manually reset: sessions remain isolated and ET-day holds
rearm automatically after exact rollover checks. ``reset`` is reserved for a
hard accounting/infrastructure latch, requires an auditable operator reason,
and never erases settled spend.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.cost_circuit import LLMCostCircuitBreaker  # noqa: E402
from src.notifier import TelegramNotifier  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "check", "reset"))
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--run-id", default="operator-cost-check")
    parser.add_argument("--mode", default="operator")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    if args.command == "reset" and not args.reason.strip():
        parser.error("reset requires --reason with a non-empty operator explanation")

    root = Path(__file__).resolve().parent.parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_config(config_path)
    db_path = Path(config.storage.db_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    breaker = LLMCostCircuitBreaker(
        str(db_path),
        config.llm_cost_circuit,
        notifier=TelegramNotifier(),
    )

    if args.command == "reset":
        # `reset` must be reachable even when the ledger fault an operator is
        # trying to clear would itself make the normal validating activation
        # path raise (2026-08-28: `activate_session()` re-seeds/validates the
        # day's accounting invariants and raised before `reset` was ever
        # dispatched, forcing a hand-run Python snippet instead of this
        # script). `reset()` reads the circuit's singleton state and the
        # emergency latch directly -- it never depends on `_seed_today` --
        # so establish the audit-trail run/mode context without it and go
        # straight to `reset`. `status`/`check` below are unaffected and
        # keep the full validating path.
        breaker.set_session_context(args.run_id, args.mode)
        breaker.reset(args.reason)
        try:
            status = breaker.status()
        except Exception as exc:
            # The reset itself is durable (it committed above); a ledger
            # fault the reset does not itself repair can still make the
            # post-reset status read fail closed. Report that plainly
            # instead of losing the fact that the reset succeeded behind an
            # uncaught traceback -- the second half of the same 2026-08-28
            # failure mode.
            print(json.dumps(
                {
                    "reset": True,
                    "reset_reason": args.reason,
                    "status_error": f"{type(exc).__name__}: {exc}",
                },
                indent=2, sort_keys=True,
            ))
            return 0
        print(json.dumps(status, indent=2, sort_keys=True, default=str))
        return 0

    breaker.activate_session(args.run_id, args.mode)
    if args.command == "check":
        breaker.enforce_current_limits(agent_name="operator_check")

    status = breaker.status()
    print(json.dumps(status, indent=2, sort_keys=True, default=str))
    if status.get("suspended"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
