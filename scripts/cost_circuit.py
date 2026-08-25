#!/usr/bin/env python3
"""Inspect, enforce, or manually reset QAMC's paid-analysis circuit.

This utility never calls a model. ``check`` is suitable for deployment: it
seeds existing same-day spend, latches if a limit is already reached, and
sends the one-time Telegram shutdown alert. ``reset`` requires an auditable
operator reason and does not erase spend, so resetting while still above a
limit will immediately re-latch on the next check.
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
    breaker.activate_session(args.run_id, args.mode)

    if args.command == "check":
        breaker.enforce_current_limits(agent_name="operator_check")
    elif args.command == "reset":
        if not args.reason.strip():
            parser.error("reset requires --reason with a non-empty operator explanation")
        breaker.reset(args.reason)

    status = breaker.status()
    print(json.dumps(status, indent=2, sort_keys=True, default=str))
    if args.command in {"status", "check"} and status.get("suspended"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
