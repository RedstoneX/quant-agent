#!/usr/bin/env python3
"""Metered smoke check for relay streaming and final usage telemetry.

This intentionally makes one tiny paid request. It uses BaseAgent's normal
streaming path and the mandatory cost circuit, so a latched/over-budget run is
blocked before network I/O and missing usage telemetry suspends paid analysis.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_env_file() -> None:
    """Best-effort load of this checkout's .env without printing secrets."""

    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--model", default="gpt-5.5")
    args = parser.parse_args()

    _load_env_file()
    from src.agents.base import BaseAgent
    from src.config import load_config
    from src.cost_circuit import PaidAnalysisSuspended, protect_paid_agent

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    config = load_config(config_path)

    class RelaySmokeAgent(BaseAgent):
        @property
        def name(self) -> str:
            return "relay_stream_smoke"

        @property
        def system_prompt(self) -> str:
            return "You are a smoke test."

        def build_user_message(self, **_kwargs) -> str:
            return "Reply with exactly: OK"

    agent = RelaySmokeAgent(
        api_key=config.api_keys.openai,
        model=args.model,
        max_tokens=512,
        provider="openai",
    )
    try:
        protect_paid_agent(
            agent,
            config,
            run_id=f"relay-smoke-{uuid.uuid4().hex[:8]}",
            mode="relay_stream_smoke",
        )
        result = agent.run()
    except PaidAnalysisSuspended as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    print(f"base_url: {os.environ.get('OPENAI_BASE_URL') or 'api.openai.com (default)'}")
    print(f"content: {result.raw_text!r}")
    print(f"finish_reason: {result.finish_reason!r}")
    print(f"usage: in={result.input_tokens} out={result.output_tokens}")
    ok = (
        bool(result.raw_text.strip())
        and result.finish_reason == "stop"
        and result.input_tokens > 0
        and result.output_tokens > 0
    )
    print(
        "RESULT: PASS — relay supports streaming + exact usage"
        if ok else
        "RESULT: FAIL — content, finish reason, or exact usage was missing"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
