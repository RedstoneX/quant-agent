"""Rolling return correlation between symbols — surfaces hidden concentration.

The hard sector cap lets NVDA (Technology) and GOOGL (Communication Services)
each take 20% of the book even though their daily returns correlate ~0.85.
If the AI theme cracks, both move together and sector diversification was an
illusion. This module quantifies what sector tags can't.
"""

import logging
import math
from typing import Iterable

import pandas as pd

from src.models import OHLCV

logger = logging.getLogger(__name__)


# NO CORRELATION CUTOFF LIVES HERE ANY MORE (board item 186, 2026-09-30).
#
# This module used to carry CLUSTER_CORRELATION_THRESHOLD = 0.7 and call any
# pair above it "one bet". The number ledger's research pass found there is no
# sourceable cutoff: published thresholded-correlation-network studies span
# roughly 0.3-0.8 and pick their value for the network density the study
# wants, and the mainstream portfolio-clustering literature does not threshold
# a correlation at all. It clusters on a correlation DISTANCE (Mantegna's
# correlation-based hierarchical clustering and its minimum spanning tree),
# which has no cutoff to choose.
#
# The owner's 2026-09-30 ruling — risk tolerance is never a global dial, it is
# read from structure or per name — makes the cutoff a defect rather than a
# value to ratify. So the cutoff is GONE and the clusters are now read from
# the book's own correlation geometry (see `correlation_clusters`).
#
# The distance below is NOT a picked level. It is a fixed point of Mantegna's
# metric d = sqrt(2 * (1 - rho)): the distance between two names with no
# linear relationship at all. It anchors the top of the gap test; it is never
# compared against a correlation to decide membership.
#
# MEASURED 2026-09-30, and the reason there is no matching anchor at the
# bottom. The first version of this cut bracketed the sorted edge lengths at
# BOTH ends, with d = 0 ("identical") below. On a realistic 11-name book with
# three real themes (within-theme corr 0.78-0.90, cross-theme 0.35-0.50) that
# lower anchor won the gap search outright and returned NO CLUSTERS AT ALL —
# a false negative in exactly the case this module exists for — and it was
# also the only source of instability: perturbing every correlation by up to
# 0.03 flipped the partition in 11.8% of 400 draws. With the lower anchor
# removed the same book returns its three themes (3/3/2) and flips in 0 of
# 400 draws, and three degenerate books (all-unrelated, all-related, all
# mid-range) flip in 0 of 400 as well.
_DISTANCE_UNRELATED = math.sqrt(2.0)  # d at rho = 0

# The WINDOW — how many bars feed the matrix — is not set here; this module
# receives already-fetched bars. It rides the caller's `trading.lookback_days`
# (see `TradingPipeline._ensure_correlation_matrix` for the recorded reason,
# board item 148): inherited from the structural-level / indicator fetch, not
# chosen for correlation, and not a number the ledger scanner can see.


def _returns_from_bars(bars: list[OHLCV]) -> pd.Series | None:
    # 21 bars → 20 returns, matching df.corr(min_periods=20) below —
    # 10-20-bar symbols used to pass this gate, land in the matrix with an
    # all-NaN (empty) row, and silently disable the cluster advisory for
    # themselves without a WARNING (audit round 2).
    if not bars or len(bars) < 21:
        return None
    closes = pd.Series([b.close for b in bars], index=[b.date for b in bars])
    returns = closes.pct_change().dropna()
    if returns.empty:
        return None
    return returns


def build_correlation_matrix(
    symbols_bars: dict[str, list[OHLCV]],
) -> dict[str, dict[str, float]]:
    """Return a nested dict {sym1: {sym2: correlation}} for the given symbol → bars map.

    Uses pairwise-complete observations. Symbols with insufficient data (< 10
    days of returns) are excluded from the matrix — they simply won't appear
    in the result map. Excluded symbols are logged at WARNING so the operator
    (and the LLM Risk Manager downstream, via prompt rendering) can see which
    holdings lost correlation coverage for the day. Without this log, a
    newly-launched ETF (e.g. CHPX with <10 bars at universe-add time) would
    silently bypass the correlation-cluster check that catches AI / mega-cap
    concentration when sector caps miss it.
    """
    returns: dict[str, pd.Series] = {}
    excluded: list[str] = []
    for sym, bars in symbols_bars.items():
        r = _returns_from_bars(bars)
        if r is not None:
            returns[sym] = r
        else:
            excluded.append(sym)
    if excluded:
        logger.warning(
            "correlation: %d symbol(s) excluded from matrix (insufficient bars, <10 returns): %s",
            len(excluded),
            ", ".join(sorted(excluded)),
        )
    if len(returns) < 2:
        return {}
    df = pd.DataFrame(returns)
    corr = df.corr(min_periods=20)  # require 20 overlapping days for any pair
    matrix: dict[str, dict[str, float]] = {}
    for sym1 in corr.columns:
        inner = {}
        for sym2 in corr.index:
            if sym1 == sym2:
                continue
            val = corr.at[sym2, sym1]
            if pd.notna(val):
                inner[sym2] = round(float(val), 3)
        matrix[sym1] = inner
    return matrix


def _correlation_distance(corr: float) -> float:
    """Mantegna's correlation distance for an ABSOLUTE correlation.

    d = sqrt(2 * (1 - |rho|)). |rho| rather than rho because this desk asks
    "do these two move together", and a -0.9 pair moves together with the sign
    flipped — the previous cutoff used |rho| for the same reason, so this
    keeps the desk's existing reading of the matrix rather than silently
    changing what counts as related.
    """
    c = min(1.0, abs(float(corr)))
    return math.sqrt(2.0 * (1.0 - c))


def _minimum_spanning_edges(
    universe: list[str],
    matrix: dict[str, dict[str, float]],
) -> list[tuple[float, str, str]]:
    """Kruskal MST (forest, if the correlation graph is disconnected).

    Pairs with no correlation entry simply have no edge — an unmeasured pair
    is not evidence of a relationship, and inventing a distance for it would
    be inventing data.
    """
    edges: list[tuple[float, str, str]] = []
    for i, sym1 in enumerate(universe):
        row = matrix.get(sym1) or {}
        for sym2 in universe[i + 1 :]:
            val = row.get(sym2)
            if val is None:
                val = (matrix.get(sym2) or {}).get(sym1)
            if val is None:
                continue
            edges.append((_correlation_distance(val), sym1, sym2))
    edges.sort()

    parent = {sym: sym for sym in universe}

    def find(sym: str) -> str:
        while parent[sym] != sym:
            parent[sym] = parent[parent[sym]]
            sym = parent[sym]
        return sym

    kept: list[tuple[float, str, str]] = []
    for dist, a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            kept.append((dist, a, b))
    return kept


def _largest_gap_cut(distances: list[float]) -> float:
    """Return the distance at which the tree's own largest gap opens.

    Edges strictly BELOW the returned value are inside a cluster; edges at or
    above it are between clusters. The cut is read off the sorted MST edge
    lengths: the single widest jump between consecutive lengths is where the
    book's correlation structure actually separates. Nothing is compared to a
    chosen level.

    The metric's "unrelated" endpoint closes the top of the list. That is what
    makes a structureless book safe without a guard constant: when the edge
    lengths carry no real jump of their own, the widest jump is the one from
    the last real edge up to "no relationship", every edge is kept, and the
    book comes back as ONE cluster. One cluster is the conservative answer —
    it rations harder, never softer — so the failure mode of an uninformative
    tree is over-rationing, not a silently split theme.

    There is deliberately NO matching anchor at the bottom: measurement showed
    it produced a false negative on a realistic themed book and was the sole
    source of session-to-session instability (see the note on
    `_DISTANCE_UNRELATED`).

    The price of the conservative fallback is visible on very small or very
    flat books: two names alone, or a book whose correlations are all alike,
    come back as one cluster even when they are only loosely related. That is
    accepted on purpose — with no structure in the tree there is nothing to
    read, and the desk would rather share a budget it need not share than
    treat one bet as two.
    """
    ordered = sorted(distances) + [_DISTANCE_UNRELATED]
    best_gap = -1.0
    cut = ordered[0]
    for lower, upper in zip(ordered, ordered[1:]):
        gap = upper - lower
        if gap > best_gap:
            best_gap = gap
            cut = upper
    return cut


def correlation_clusters(
    symbols: Iterable[str],
    matrix: dict[str, dict[str, float]],
) -> list[list[str]]:
    """Group `symbols` into clusters read from the correlation geometry itself.

    Method: Mantegna correlation-distance single-linkage clustering. Build the
    minimum spanning tree of d = sqrt(2 * (1 - |corr|)), then cut it at the
    largest gap in its own sorted edge lengths. There is no correlation cutoff
    and no picked distance: the only two constants are the metric's values at
    |corr| = 1 and corr = 0, which bracket the gap search.

    STILL TRANSITIVE, deliberately. Single linkage chains: if A~B and B~C the
    three are one cluster even when A and C are not close to each other. That
    is the right reading for this desk — `OKLO / CEG / VST / CCJ` is one
    nuclear bet transmitted through the theme, not three independent ones —
    and it is what the old cutoff did too, so rationing does not loosen. The
    difference is that chaining is now limited by the data: a chain only forms
    through links that are tight RELATIVE TO THE REST OF THE BOOK, where the
    fixed 0.7 let a chain run as far as the number allowed.

    This is rationing, never a diversification requirement: it only ever says
    "these names share a budget", and it never asks the desk to buy something
    to spread out.

    Singletons are omitted. Clusters are sorted largest first, each member
    list alphabetical, so rendering is stable across runs.
    """
    universe = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if len(universe) < 2 or not matrix:
        return []

    mst = _minimum_spanning_edges(universe, matrix)
    if not mst:
        return []

    cut = _largest_gap_cut([dist for dist, _, _ in mst])

    parent = {sym: sym for sym in universe}

    def find(sym: str) -> str:
        while parent[sym] != sym:
            parent[sym] = parent[parent[sym]]
            sym = parent[sym]
        return sym

    for dist, a, b in mst:
        if dist < cut:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for sym in universe:
        groups.setdefault(find(sym), []).append(sym)
    clusters = [sorted(members) for members in groups.values() if len(members) > 1]
    clusters.sort(key=lambda members: (-len(members), members[0]))
    return clusters


def cluster_peers(
    symbol: str,
    candidates: Iterable[str],
    matrix: dict[str, dict[str, float]],
) -> list[str]:
    """The other members of `symbol`'s cluster, drawn from `candidates`.

    Drop-in replacement for the old `highly_correlated_peers` call in the risk
    rules: same shape (a sorted list of OTHER symbols), but membership comes
    from the structural clustering above instead of a pairwise cutoff. The
    symbol itself is excluded, exactly as before, so callers that add it back
    for exposure accounting keep working.
    """
    sym = str(symbol).strip().upper()
    pool = {sym} | {str(c).strip().upper() for c in candidates if str(c).strip()}
    for cluster in correlation_clusters(pool, matrix):
        if sym in cluster:
            return [m for m in cluster if m != sym]
    return []


def highly_correlated_peers(
    symbol: str,
    candidates: Iterable[str],
    matrix: dict[str, dict[str, float]],
    threshold: float,
) -> list[str]:
    """Of `candidates`, the ones whose |correlation| with `symbol` is >= `threshold`.

    `threshold` is now REQUIRED and has no module default, because the desk
    has no cutoff to default to. Risk rationing uses `cluster_peers`; this
    remains only for callers that are answering an explicit "who is above X"
    question with an X of their own.
    """
    row = matrix.get(symbol, {})
    return [peer for peer in candidates if peer != symbol and abs(row.get(peer, 0.0)) >= threshold]
