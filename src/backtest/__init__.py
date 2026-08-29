"""Deterministic-layer backtester.

See `src/backtest/engine.py` for the full scope statement. In one sentence:
this measures stop placement, noise-band widening, risk-based sizing, the
portfolio risk budget / cluster caps, and the trailing-stop rules against
real history — it does NOT replay the LLM agents, whose outputs are not
reproducible (docs/QAMC_REMEDIATION_SPEC.md §7.1).
"""
