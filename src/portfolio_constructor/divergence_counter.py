"""Counts every computed-target vs analyst-target comparison, and how the
`target_divergence_warn_pct` threshold fell across them.

Why this exists. The ledger row for
`src.config.RiskConfig.target_divergence_warn_pct` (= 25) routes to a
recording that was SPECIFIED and never built: nothing in the repo observed
the gap distribution, so the threshold could only be argued about. The
comparison site (`PortfolioConstructor._log_target_divergence`) emitted one
log line per symbol and kept no count, so after a run nobody could say how
many comparisons happened, let alone what share of them crossed 25%.

This module adds counting only. It changes no value, gates nothing, and
places no order. After one session the log carries the histogram, and the
threshold can be set at a stated percentile of observed gaps instead of a
round number. A counter is not a settlement.

The buckets are absolute percentage gaps. They are deliberately finer than
the current threshold so the shape either side of 25 is readable rather
than assumed.
"""
from __future__ import annotations

import logging
from threading import Lock

logger = logging.getLogger(__name__)

#: Upper edges, in absolute percent, of the gap histogram. The last bucket
#: is open-ended. Chosen to bracket the current 25 threshold from both
#: sides; they are reporting bins, not a gate, so no value depends on them.
BUCKET_EDGES: tuple[float, ...] = (5.0, 10.0, 15.0, 20.0, 25.0, 35.0, 50.0, 100.0)

#: Emit the cumulative summary every Nth comparison, so a run's log always
#: carries a recent total even if no warn ever fires.
SUMMARY_EVERY = 10

_lock = Lock()
_counts: dict[str, int] = {}


def _bucket(abs_gap: float) -> str:
    low = 0.0
    for edge in BUCKET_EDGES:
        if abs_gap < edge:
            return f"{low:g}-{edge:g}%"
        low = edge
    return f">={low:g}%"


def reset() -> None:
    """Clear the counts. For tests and for a per-process fresh start."""
    with _lock:
        _counts.clear()


def snapshot() -> dict[str, int]:
    """The counts so far, as a plain dict."""
    with _lock:
        return dict(_counts)


def summary_line() -> str:
    """One readable line carrying every count collected so far."""
    snap = snapshot()
    total = snap.get("comparisons", 0)
    warned = snap.get("warned", 0)
    buckets = {k: v for k, v in sorted(snap.items()) if k.endswith("%")}
    parts = " ".join(f"{name}={count}" for name, count in buckets.items())
    return (
        f"target-divergence counter: comparisons={total} warned={warned} "
        f"({(100.0 * warned / total) if total else 0.0:.1f}% of comparisons) "
        f"buckets[{parts}]"
    )


def observe(*, gap_pct: float, threshold_pct: float) -> None:
    """Count one comparison, and whether it crossed `threshold_pct`.

    `gap_pct` is signed; the sign is counted separately so a one-directional
    bias in the analyst's targets is visible, which is the finding the row's
    open question actually asks for.
    """
    abs_gap = abs(gap_pct)
    warned = abs_gap >= threshold_pct
    with _lock:
        _counts["comparisons"] = _counts.get("comparisons", 0) + 1
        if warned:
            _counts["warned"] = _counts.get("warned", 0) + 1
        direction = "above" if gap_pct > 0 else ("below" if gap_pct < 0 else "equal")
        key = f"analyst_{direction}"
        _counts[key] = _counts.get(key, 0) + 1
        name = _bucket(abs_gap)
        _counts[name] = _counts.get(name, 0) + 1
        total = _counts["comparisons"]
    if warned or total % SUMMARY_EVERY == 0:
        logger.info("%s", summary_line())


def log_divergence(*, symbol: str, derivation, threshold_pct: float) -> None:
    """Record where the model's guess and the computed level disagree.

    Lifted unchanged from `PortfolioConstructor._log_target_divergence`
    (2026-10-05), with the counter call added. The model's target is no
    longer arithmetic, but it is still the only read available on whether
    the model's chart-reading is worth anything. A large, one-directional
    gap across many symbols is a finding about the seat; a large gap on one
    symbol is a finding about that symbol -- and until now neither could be
    read, because nothing counted the comparisons.
    """
    if derivation.price is None or derivation.model_target is None:
        return
    gap = derivation.divergence_pct
    if gap is None:
        return
    observe(gap_pct=gap, threshold_pct=threshold_pct)
    message = (
        "Constructor: %s target -- computed $%.2f (%s) vs analyst's "
        "reference_target $%.2f: %+.1f%%"
    )
    args = (
        symbol, derivation.price, derivation.basis,
        derivation.model_target, gap,
    )
    if abs(gap) >= threshold_pct:
        logger.warning(message + " -- the model and the chart disagree sharply", *args)
    else:
        logger.info(message, *args)
