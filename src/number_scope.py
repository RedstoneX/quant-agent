"""The scope of the number-source guard: modules on the path to a broker order.

Relocated verbatim from ``src/number_sources.py`` so that file shrinks instead
of growing with each module split that adds an entry here. Data only: a tuple
of repository-relative paths, constructible with nothing else imported.
"""

from __future__ import annotations

__all__ = ["SCOPED_PATHS"]

#: The modules on the path from a verdict to a broker order. See the SCOPE
#: rule in the module docstring; this list is the rule applied, and
#: `scripts/unscoped_number_guard.py` is what stops it from silently lagging.
#: A directory entry covers every `.py` under it.
SCOPED_PATHS: tuple[str, ...] = (
    "src/risk",
    "src/portfolio_constructor",
    "src/rotation.py",
    "src/infra_retry_policy.py",
    "src/nominations.py",
    "src/evidence_gate.py",
    "src/verdicts.py",
    "src/data/correlation.py",
    # 2026-10-01: the sector cluster moved out of src/execution/broker.py
    # verbatim (sector resolution feeds the exposure ladder); same code, same scope.
    "src/sector_reference.py",
    # 2026-09-19, board item 124: the research-defined insider purchase
    # cluster now lifts the smart-money seat's conviction, so its definition
    # is on the path from a verdict to an order.
    "src/data/smart_money_cluster.py",
    # The indicator and level units. Every ATR multiple and every stop the
    # ledger tracks is a multiple of `technical.ATR_PERIOD`, and levels are
    # where stops are placed; watching the multiplier and not the unit was
    # the gap the scope rule above was written to close.
    "src/data/technical.py",
    "src/data/levels.py",
    # Decides whether an APPROVED trade is actually sent
    # (`MAX_ENTRY_SLIPPAGE_BPS`), and carries the sizing fallback.
    "src/pipeline_stages.py",
    # 2026-10-01, board item 210 step 10: the four stage classes moved out of
    # `src/pipeline_stages.py` verbatim. Same code, same scope -- these paths
    # keep their numbers inside the ledger instead of dropping out silently.
    "src/stage_morning_research.py",
    "src/stage_decision.py",
    "src/stage_risk.py",
    "src/stage_execution.py",
    "src/pipeline_sizing.py",
    "src/pipeline_earnings_quality.py",
    # 2026-10-01, board item 210 step 12: the rotation-EXECUTION block and
    # the entry order-placement/re-peg block moved out of
    # `src/pipeline_stages.py` verbatim. Same code, same scope.
    "src/pipeline_rotation_exec.py",
    "src/pipeline_entry_orders.py",
    "src/execution/cash_sweep.py",
    "src/execution/stop_records.py",
    # 2026-09-19, board item 130: `broker.py` IS the broker order -- the
    # scope rule's own words ("every module on the path from a seat's
    # verdict to a broker order") named this file and it was not here.
    # `stop_repair.py` and `coverage_watchdog.py` are the repair/alarm path
    # for a protective stop that failed to place. `stop_repair.py` still
    # defines no module-level numeric constant (see the docstring note this
    # entry used to require); scoping it adds nothing today but stops a
    # future one arriving unseen.
    "src/execution/broker.py", "src/execution/broker_parts",
    "src/execution/stop_repair.py", "src/execution/order_gates.py", "src/execution/order_idempotency.py",
    "src/coverage_watchdog.py",
    # The pipeline's own decision/execution glue. The de-lever and midday
    # order-price buffers are inline multipliers and rule (e) has seen them
    # since 2026-09-19; rule (c) (function-parameter defaults) was added the
    # same day for `_clamp_queued_earnings_buys`' `max_pct=5.0`, which no
    # longer exists — that gate refuses the BUY instead of sizing it (board
    # item 186, 2026-10-01) — and the rule stays because the shape recurs.
    "src/pipeline.py",
    "src/pipeline_delever.py", "src/delever/forced.py", "src/delever/ladder.py", "src/delever/conviction.py",
    "src/delever/enforce.py", "src/delever/trims.py", "src/delever/risk_number.py",
    # The held-position exit engine and the exit-trigger vocabulary -- moved
    # here out of `src/pipeline.py` by step 4 of docs/PIPELINE_SPLIT_PLAN.md.
    # Every trail multiple and every exit threshold it carries stays scoped.
    "src/pipeline_exits.py", "src/exits/exit_records.py",  # the trail cooldown lifted verbatim 2026-10-04
    # The intra-check session and the intraday opportunity scan -- moved here
    # out of `src/pipeline.py` by step 8 of docs/PIPELINE_SPLIT_PLAN.md.
    "src/pipeline_intraday.py",
    # 2026-10-01, board item 210 step 6: the universe-admission cluster --
    # the external-nomination gates, the screen and its admission -- moved
    # here out of `src/pipeline.py`. Its dollar-volume and price floors stay
    # scoped.
    "src/pipeline_admission.py",
    "src/pipeline_prompt_facts.py", "src/pipeline_prompt_facts_pure.py", "src/pipeline_prompt_facts_review.py", "src/prompt_facts/missed_ops_signals.py", "src/prompt_facts/review/grading.py", "src/prompt_facts/review/exits.py", "src/prompt_facts/review/calibration.py", "src/prompt_facts/review/blocked.py", "src/prompt_facts/review/replay.py", "src/prompt_facts/decisions.py", "src/prompt_facts/projected.py", "src/prompt_facts/watchlist.py", "src/prompt_facts/heat.py", "src/prompt_facts/pm_facts.py",
    # Step 5 of docs/PIPELINE_SPLIT_PLAN.md (board item 210): risk-verdict
    # application moved here out of `src/pipeline.py`.
    "src/pipeline_risk_gate.py",
    # 2026-10-01, board item 210 step 2: the protection cluster -- stop
    # coverage, repair, protected sells, write-ahead restore, the fill and
    # stop-out reconcilers -- moved here out of `src/pipeline.py`. Scoped at
    # its new address so its numbers stay under the guard.
    "src/pipeline_protection.py", "src/protection/protected_sell.py", "src/protection/reprotect_records.py",
    # Every seat's prompt-construction and LLM-call code -- the path from
    # evidence to a seat's verdict the scope rule names. Most of what lives
    # here is LLM plumbing (timeouts, retries, token budgets) that is
    # `not-trade-governing` once seen; the truncation caps and rank tables
    # that shape what evidence a verdict is built from are not.
    "src/agents",
    # 2026-09-19: the universe admission screen. Every threshold that decides
    # whether a symbol may be traded at all lives here or in
    # `UniverseScreenConfig`.
    "src/universe_screen.py",
)
