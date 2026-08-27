"""Correlation-aware risk-budget allocation — spec §2.2, and the gate §2.1 sizes into.

Phase 2a made the book's risk *measurable* (`src/risk/metrics.py`): budget risk,
open risk, headroom under `risk.max_portfolio_risk_pct`. Every one of those was
a reported figure — the Portfolio Manager could read the ceiling and ignore it,
because nothing consumed the number. This module is what consumes it.

Two ceilings, and the second is the one that does the real work:

**Total.** The sum of every position's loss-if-stopped may not exceed
`ceiling_pct` of equity (owner-ratified at 25%). Note what this rations: RISK,
not notional. Capital is meant to be fully deployed.

**Per cluster.** Correlated names consume ONE bet's budget, not several. A book
holding OKLO, CEG, VST and CCJ at 5% risk each is not carrying four 5% bets; it
is carrying one 20% bet on the nuclear theme with extra commission. Without a
cluster cap the total ceiling is trivially satisfiable by a book that is
concentrated in exactly the way the ceiling exists to prevent, which is why
"total risk is under 25%" was never on its own a meaningful statement about
diversification. A single cluster may take at most `cluster_share_pct` of the
total ceiling (suggested 40%, i.e. 10% of equity at a 25% ceiling).

Clusters arrive from `src/data/correlation.py::correlation_clusters` — measured
return correlation over five years, transitive, thresholded. They are not a
hand-maintained sector table, so a theme that trades together is caught whether
or not anyone thought to name it.

**Rationing rule.** When a ceiling binds, the largest request is served first
and later ones take the remainder. Requests are ordered by requested risk
descending, ties broken alphabetically so the outcome never depends on dict
ordering or the order the PM happened to list its targets. A request cut below
`floor_pct` is DENIED rather than shrunk to a token position: below the floor
the idea is not worth trading, and a 0.1%-risk position pays full commission
and full attention for an immaterial payoff.

**Held positions are not rationed.** A symbol already in the book consumes its
existing risk whether or not this session names it. The budget available to new
ideas is what is left after that, and the way to release it is for a stop to
reach entry (spec §2.3) or for the position to be sold — never for the
allocator to pretend an open position is not there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "RiskRequest",
    "RiskGrant",
    "BudgetAllocation",
    "allocate_risk_budget",
]


@dataclass(frozen=True)
class RiskRequest:
    """A symbol asking to carry `requested_pct` of equity at risk.

    This is the position's TOTAL intended risk, not a delta. A request below a
    symbol's existing risk is a trim, and the allocator neither blocks it nor
    charges for it — reducing risk never needs budget.
    """

    symbol: str
    requested_pct: float


@dataclass(frozen=True)
class RiskGrant:
    """What the allocator actually permits, and why it differs."""

    symbol: str
    requested_pct: float
    granted_pct: float
    #: Set when granted < requested. One of "total_ceiling", "cluster_cap",
    #: "below_floor". None when the request was served in full.
    limited_by: str | None = None
    #: Cluster the symbol was rationed within, when a cluster cap applied.
    cluster: tuple[str, ...] | None = None
    #: Operator-readable note. Carried into the order's reasoning so the AI
    #: Risk Manager reads a deterministic cut as arithmetic rather than as the
    #: PM contradicting itself — the failure mode of 2026-08-20, where a
    #: constructor cap read as "plan inconsistency" and drew a full-plan veto.
    note: str = ""

    @property
    def denied(self) -> bool:
        return self.granted_pct <= 0.0 < self.requested_pct


@dataclass(frozen=True)
class BudgetAllocation:
    """Every grant, plus what the book looks like once they are applied."""

    grants: dict[str, RiskGrant]
    #: Total risk % of equity committed after allocation, held + granted.
    committed_pct: float
    ceiling_pct: float
    #: Risk % per cluster after allocation, keyed by the cluster tuple.
    cluster_pct: dict[tuple[str, ...], float]

    @property
    def headroom_pct(self) -> float:
        return round(max(0.0, self.ceiling_pct - self.committed_pct), 4)

    def granted(self, symbol: str) -> float:
        grant = self.grants.get(symbol.upper())
        return grant.granted_pct if grant else 0.0


def _clean_pct(value: object) -> float:
    """Coerce to a finite non-negative float. Broker and LLM inputs carry NaN."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(out) or out < 0:
        return 0.0
    return out


def allocate_risk_budget(
    requests: list[RiskRequest],
    *,
    existing_pct: dict[str, float] | None = None,
    clusters: list[list[str]] | None = None,
    ceiling_pct: float = 25.0,
    cluster_share_pct: float = 40.0,
    floor_pct: float = 0.5,
) -> BudgetAllocation:
    """Ration `requests` under the total and per-cluster risk ceilings.

    `existing_pct` maps symbol → risk % of equity the book already carries
    there. A symbol that also appears in `requests` is being RE-sized, so its
    existing risk is replaced by the grant rather than added to it — otherwise
    holding a name would make adding to it cost double.

    `clusters` is `correlation_clusters()` output: groups of correlated
    symbols, singletons omitted. A symbol in no cluster is rationed only by
    the total ceiling.

    Returns grants for every request (including denials, so the caller can
    explain a missing order) and the resulting committed risk.
    """
    existing = {
        str(sym).strip().upper(): _clean_pct(pct)
        for sym, pct in (existing_pct or {}).items()
        if str(sym).strip()
    }
    ceiling = max(0.0, _clean_pct(ceiling_pct))
    floor = max(0.0, _clean_pct(floor_pct))
    # A cluster may take at most this share of the TOTAL ceiling, expressed as
    # a percentage of equity so it is comparable with everything else here.
    share = min(100.0, max(0.0, _clean_pct(cluster_share_pct)))
    cluster_ceiling = ceiling * share / 100.0

    cluster_of: dict[str, tuple[str, ...]] = {}
    for members in clusters or []:
        key = tuple(sorted({str(m).strip().upper() for m in members if str(m).strip()}))
        if len(key) < 2:
            continue  # a symbol correlated with nothing is not a cluster
        for member in key:
            cluster_of[member] = key

    # Normalise requests. A later duplicate of the same symbol replaces the
    # earlier one — PM emitting a symbol twice is a malformed decision, and
    # summing the two would silently double its size.
    by_symbol: dict[str, float] = {}
    for req in requests:
        symbol = str(req.symbol).strip().upper()
        if symbol:
            by_symbol[symbol] = _clean_pct(req.requested_pct)

    # Held positions this session did not name keep consuming their risk.
    committed = sum(pct for sym, pct in existing.items() if sym not in by_symbol)
    cluster_committed: dict[tuple[str, ...], float] = {}
    for sym, pct in existing.items():
        if sym in by_symbol:
            continue
        key = cluster_of.get(sym)
        if key is not None:
            cluster_committed[key] = cluster_committed.get(key, 0.0) + pct

    # Largest request first, alphabetical tie-break: when the budget binds,
    # conviction is served before the remainder is shared out, and the result
    # never depends on the order PM listed its targets in.
    ordered = sorted(by_symbol.items(), key=lambda kv: (-kv[1], kv[0]))

    grants: dict[str, RiskGrant] = {}
    for symbol, requested in ordered:
        key = cluster_of.get(symbol)
        if requested <= 0.0:
            # A zero request is PM closing the name. It consumes no budget and
            # is never "denied" — there is nothing to deny.
            grants[symbol] = RiskGrant(symbol, requested, 0.0)
            continue

        total_headroom = max(0.0, ceiling - committed)
        limits: list[tuple[float, str]] = [(total_headroom, "total_ceiling")]
        if key is not None:
            limits.append(
                (max(0.0, cluster_ceiling - cluster_committed.get(key, 0.0)),
                 "cluster_cap"),
            )
        allowed, binding = min(limits, key=lambda item: item[0])

        if requested <= allowed:
            grants[symbol] = RiskGrant(symbol, requested, round(requested, 4))
            committed += requested
            if key is not None:
                cluster_committed[key] = cluster_committed.get(key, 0.0) + requested
            continue

        granted = round(allowed, 4)
        if granted < floor:
            # Below the floor the idea is not worth trading. A token position
            # pays full commission and full attention for an immaterial payoff.
            grants[symbol] = RiskGrant(
                symbol, requested, 0.0, limited_by="below_floor", cluster=key,
                note=(
                    f"[risk budget: {symbol} denied — {binding.replace('_', ' ')} "
                    f"leaves {granted:.2f}% risk available, under the "
                    f"{floor:.2f}% minimum. Deterministic, not PM inconsistency]"
                ),
            )
            continue

        if binding == "cluster_cap" and key is not None:
            detail = (
                f"cluster {'/'.join(key)} capped at {cluster_ceiling:.2f}% of "
                f"equity ({share:.0f}% of the {ceiling:.2f}% total) — correlated "
                f"names consume one bet's budget"
            )
        else:
            detail = (
                f"total at-risk ceiling {ceiling:.2f}% of equity leaves "
                f"{granted:.2f}%"
            )
        grants[symbol] = RiskGrant(
            symbol, requested, granted, limited_by=binding, cluster=key,
            note=(
                f"[risk budget: {symbol} cut from {requested:.2f}% to "
                f"{granted:.2f}% risk — {detail}. Deterministic, not PM "
                f"inconsistency]"
            ),
        )
        committed += granted
        if key is not None:
            cluster_committed[key] = cluster_committed.get(key, 0.0) + granted

    return BudgetAllocation(
        grants=grants,
        committed_pct=round(committed, 4),
        ceiling_pct=round(ceiling, 4),
        cluster_pct={k: round(v, 4) for k, v in cluster_committed.items()},
    )
