"""Shared state for the per-symbol phases of `_midday_execute_llm_actions`.

The per-symbol loop of `ExitEngineMixin._midday_execute_llm_actions`
(src/pipeline_exits.py) is split into phase functions in this package. A
phase returns `SKIP` exactly where the loop body used to `continue`; the
caller writes `if phase(...) is SKIP: continue`, so control flow is
unchanged. `MiddayLoop` carries the values that are the same for every
symbol of one pass, so no phase takes more than four arguments.
"""
from dataclasses import dataclass


class _Skip:
    """Sentinel type: the phase refused this symbol (the old `continue`)."""

    def __repr__(self) -> str:
        return "SKIP"


SKIP = _Skip()


@dataclass(frozen=True)
class MiddayLoop:
    """Loop invariants of one `_midday_execute_llm_actions` pass."""

    owner: object
    positions: list
    run_id: str
    metric_deltas: dict | None
    risk_vetoed_symbols: set | None
    position_facts: dict | None
