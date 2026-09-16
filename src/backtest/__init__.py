"""Deterministic-layer backtester.

See `src/backtest/engine.py` for the full scope statement. In one sentence:
this measures stop placement, noise-band widening, risk-based sizing, and
the trailing-stop rules against real history. The portfolio risk budget
runs, but a binding day is served alphabetically (no ranking, equal asks)
and every result reports that count — it does NOT replay the LLM agents,
whose outputs are not reproducible (docs/QAMC_REMEDIATION_SPEC.md §7.1).
"""
