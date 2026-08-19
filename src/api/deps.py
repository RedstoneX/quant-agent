"""Shared, narrow config/roster access for the API — no execution imports.

Deliberately does NOT expose `AppConfig`/`ApiKeysConfig` to route handlers.
Callers pull only the specific non-secret fields they need through the
functions below, so there is no object floating around the route layer that
`.model_dump()` could ever accidentally leak (see `ApiKeysConfig` in
src/config.py, which holds every LLM/Alpaca/FRED key in plaintext).

This module imports `src.config` only (pure config parsing — not trading
execution). It never imports `src.pipeline`, `src.pipeline_stages`,
`src.execution.broker`'s write path, or `src.risk.*`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from src.config import AGENT_NAMES, AppConfig, load_config

# Display-only duplicate of src/risk/rules.py::_ETF_LEVERAGE's negative-
# multiplier entries (the inverse/short ETFs already in the trading
# universe). src/api may never import src.risk (tests/test_api_safety.py
# enforces this structurally), so this small labeling set is reimplemented
# here rather than shared — it only tags directional character for
# display, it computes no exposure/sizing/risk math. Keep in sync by hand
# if the risk-engine list changes.
INVERSE_ETF_SYMBOLS: frozenset[str] = frozenset({"SH", "SDS", "PSQ", "SQQQ"})

# Agent roles are a static, documented fact about the system (CLAUDE.md's
# "Agent CoT structure" table) — safe to hardcode here rather than derive.
AGENT_ROLES: dict[str, str] = {
    "tech_analyst": "Technical analysis research agent",
    "news_analyst": "News/geopolitical research agent",
    "macro_analyst": "Macro regime research agent (owns the regime enum)",
    "earnings_analyst": "Earnings/filing research agent",
    "portfolio_manager": "Portfolio Manager — per-symbol target weights + conviction",
    "risk_manager": "AI Risk Manager — advisory review of PM proposal (protected agent)",
    "position_reviewer": "Midday/close sell-side reviewer (protected agent)",
    "evening_analyst": "Evening reflection + tomorrow prep",
    "meta_reflector": "Quarterly self-audit + prompt-edit proposal agent",
}


def _config_path() -> Path:
    return Path(os.environ.get("QUANT_AGENT_API_CONFIG", "config/settings.yaml"))


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load config once per process. Cached — the API is a short-lived read
    process; a config change takes effect on the next API restart, same as
    the trading process picking up settings.yaml only at its own startup."""
    return load_config(_config_path())


def get_db_path() -> str:
    return get_config().storage.db_path


def get_alpaca_paper() -> bool:
    return get_config().alpaca.paper


def get_alpaca_credentials() -> tuple[str, str]:
    cfg = get_config()
    return cfg.api_keys.alpaca_key, cfg.api_keys.alpaca_secret


def get_cash_sweep_enabled() -> bool:
    return get_config().cash_sweep.enabled


def get_cash_sweep_symbol() -> str:
    return get_config().cash_sweep.symbol


def get_cash_sweep_reserve_pct() -> float:
    return get_config().cash_sweep.reserve_pct


def get_agent_model(agent_name: str) -> str | None:
    return getattr(get_config().llm, f"{agent_name}_model", None)


def get_agent_provider(agent_name: str) -> str | None:
    """Explicit override only — mirrors resolve_provider()'s `None` = prefix-inferred
    convention from src/agents/base.py; does not re-run prefix inference here."""
    return getattr(get_config().llm, f"{agent_name}_provider", None)


def agent_roster() -> list[dict]:
    return [
        {
            "agent_name": name,
            "role": AGENT_ROLES.get(name, ""),
            "configured_model": get_agent_model(name),
            "configured_provider": get_agent_provider(name),
        }
        for name in AGENT_NAMES
    ]
