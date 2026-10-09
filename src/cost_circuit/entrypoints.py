"""src.cost_circuit.entrypoints -- moved verbatim from src/cost_circuit.py; see the package docstring."""

from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, TypeVar
from src.cost_circuit.breaker import LLMCostCircuitBreaker


def activate_paid_call_session(
    app_config: Any,
    *,
    run_id: str,
    mode: str,
    notifier: Any | None = None,
    db_path: str | Path | None = None,
) -> LLMCostCircuitBreaker:
    """Construct and activate the mandatory breaker for operator paid tools.

    Application services construct their breaker inside ``TradingPipeline``.
    Standalone replays, benchmarks, smoke checks, and commissioning probes do
    not instantiate that pipeline, so they must use this common boundary
    instead of silently calling an SDK or BaseAgent without accounting.
    """

    raw_db_path = str(db_path or app_config.storage.db_path)
    if raw_db_path == ":memory:":
        resolved_db_path = raw_db_path
    else:
        resolved = Path(raw_db_path)
        if not resolved.is_absolute():
            resolved = Path(__file__).resolve().parent.parent.parent / resolved
        resolved_db_path = str(resolved)
    try:
        breaker = LLMCostCircuitBreaker(
            resolved_db_path,
            app_config.llm_cost_circuit,
            notifier=notifier,
        )
    except Exception as exc:
        breaker = LLMCostCircuitBreaker.fail_closed(
            resolved_db_path,
            app_config.llm_cost_circuit,
            exc,
            notifier=notifier,
            run_id=run_id,
            mode=mode,
            agent_name="circuit_startup",
        )
    try:
        from src.cost_table import refresh_openrouter_pricing

        # Pricing-staleness SPOF fix (2026-08-28): pass the configured grace
        # window/multiplier through explicitly so a stale-but-recent cache
        # is used (widened, logged loudly) instead of latching this whole
        # process the moment openrouter.ai is briefly unreachable -- see the
        # long note above `refresh_openrouter_pricing` in src/cost_table.py.
        pricing_ok = refresh_openrouter_pricing(
            grace_period_hours=float(
                getattr(
                    app_config.llm_cost_circuit,
                    "openrouter_pricing_grace_period_hours",
                    0.0,
                )
            ),
            max_stale_multiplier=float(
                getattr(
                    app_config.llm_cost_circuit,
                    "openrouter_pricing_stale_multiplier_max",
                    1.0,
                )
            ),
        )
    except Exception as exc:
        breaker.mark_unavailable(
            exc,
            run_id=run_id,
            mode=mode,
            agent_name="pricing_preflight",
            attempts=0,
        )
    else:
        if not pricing_ok:
            breaker.mark_unavailable(
                RuntimeError("current official OpenRouter pricing is unavailable; paid calls cannot be bounded safely"),
                run_id=run_id,
                mode=mode,
                agent_name="pricing_preflight",
                attempts=0,
            )
    try:
        breaker.activate_session(run_id, mode)
    except Exception as exc:
        breaker.mark_unavailable(
            exc,
            run_id=run_id,
            mode=mode,
            agent_name="circuit_activation",
        )
    return breaker


def protect_paid_agent(
    agent: Any,
    app_config: Any,
    *,
    run_id: str,
    mode: str,
    notifier: Any | None = None,
    db_path: str | Path | None = None,
) -> LLMCostCircuitBreaker:
    """Activate one operator-tool session and attach it to a BaseAgent."""

    breaker = activate_paid_call_session(
        app_config,
        run_id=run_id,
        mode=mode,
        notifier=notifier,
        db_path=db_path,
    )
    agent.set_cost_circuit(breaker)
    breaker.require_paid_analysis(f"{mode}_start")
    return breaker
