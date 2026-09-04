"""Shared materiality/cluster-window admission logic for smart-money rows.

Both `SECForm4Provider` (SEC Form 4, `stream="insider"`,
src/data/smart_money.py) and `CongressionalTradingProvider` (Congress
disclosures, `stream="congressional"`, src/data/congressional_trading.py)
reduce their candidate observations the same way: keep any single row that
alone clears a materiality threshold, plus any independent-actor cluster
within a shared lookback window whose combined value clears that threshold.

This was previously inlined once, inside `SECForm4Provider.fetch`. It is
extracted here so the cluster window itself (Alldredge & Blank, J. Financial
Research 2019: ~2 days — see docs/RESEARCH_FINDINGS.md:19, and the
2026-09-04 14->2 day audit fix) cannot silently drift between the two
sources by one of them growing its own copy.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from src.models import SmartMoneyObservation


def observation_key(item: SmartMoneyObservation) -> tuple:
    return (
        item.accession_number,
        item.transaction_row,
        item.symbol,
        item.actor_cik,
        item.transaction_date,
        item.transaction_code,
    )


def cluster_survivors(
    items: list[SmartMoneyObservation],
    *,
    threshold_fn: Callable[[str], float],
    cluster_window_days: int,
    min_cluster_owners: int,
) -> dict[tuple, SmartMoneyObservation]:
    """Return the subset of ``items`` that clears materiality alone or as a
    cluster of independent actors, keyed by :func:`observation_key`.

    ``threshold_fn(symbol)`` returns the USD materiality threshold for that
    symbol (core vs. external universes may use different thresholds — see
    each provider's own ``fetch``). An amendment is retained in the raw
    cache for provenance but cannot independently clear either gate, so an
    original filing and its correction are never counted as two actions.
    """
    by_group: dict[tuple[str, str], list[SmartMoneyObservation]] = defaultdict(list)
    for item in items:
        if item.amendment:
            continue
        by_group[(item.symbol, item.direction)].append(item)

    survivors: dict[tuple, SmartMoneyObservation] = {}
    for (symbol, _direction), group in by_group.items():
        threshold = threshold_fn(symbol)
        for item in group:
            if item.transaction_value_usd is not None and item.transaction_value_usd >= threshold:
                survivors[observation_key(item)] = item
        for anchor in group:
            window = [
                item for item in group
                if abs((item.transaction_date - anchor.transaction_date).days)
                <= cluster_window_days
                and item.transaction_value_usd is not None
            ]
            independent = {item.actor_cik for item in window if item.actor_cik}
            total = sum(item.transaction_value_usd or 0 for item in window)
            if len(independent) >= min_cluster_owners and total >= threshold:
                for item in window:
                    survivors[observation_key(item)] = item
    return survivors
