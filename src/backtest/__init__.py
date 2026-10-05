"""Deterministic-layer backtester.

See `src/backtest/engine.py` for the full scope statement. In one sentence:
this measures stop placement, noise-band widening, risk-based sizing, and
the trailing-stop rules against real history. The portfolio risk budget
runs, but a binding day is served alphabetically (no ranking, equal asks)
and every result reports that count — it does NOT replay the LLM agents,
whose outputs are not reproducible (docs/QAMC_REMEDIATION_SPEC.md §7.1).
"""

from src.backtest import swept_values as _swept_values

#: Importing this engine installs the swept-value read counters (see
#: `swept_values`): value-identical wrappers that record, per run, how many
#: times each value a sweep can vary was actually READ at its use site. A
#: byte-identical A/B with a zero read count is an unreached value, not an
#: inert one — this package has been fooled by that four times.
_swept_values.install()
