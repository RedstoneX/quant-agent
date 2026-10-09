"""Shared plumbing for the pieces lifted out of `ExecutionStage._run_session`.

`SKIP` is what a lifted per-name body returns where the loop body it came from
said `continue`; the caller turns it straight back into that `continue`, so
the loop visits names in exactly the same order and skips exactly the same
ones. `EntryRun` carries the entry loop's per-run invariants (set once before
the loop and never re-bound inside it) and `EntryLeg` the per-name facts one
lifted entry phase reads, so no lifted function needs more than six
parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SKIP = object()


@dataclass(frozen=True)
class EntryRun:
    """Per-run invariants of the entry submit loop."""

    pipeline: Any
    ctx: Any
    submit_queue: list
    deferred_far_through: set
    original_entry_count: int
    budget_is_gross: bool
    total_value: Any


@dataclass(frozen=True)
class EntryLeg:
    """Per-name facts the quote/limit phase reads for one queued entry."""

    decision: Any
    is_short: bool
    queue_index: int
    market_price: Any
    ask: Any
    bid: Any


@dataclass(frozen=True)
class SellLeg:
    """Per-name facts the rotation-close record reads for one submitted SELL."""

    decision: Any
    qty: Any
    sell_limit: Any
    rotation_final_reason: Any
