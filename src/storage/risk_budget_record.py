"""Shape the board item 186 concentration recording's row. Pure.

RECORDING ONLY. Nothing here decides anything: it turns the risk-budget
allocator's own output into the values `realised_risk_budget` stores, so the
storage layer holds the INSERT and nothing else. The ceilings this makes
observable (`max_portfolio_risk_pct` 25, `max_cluster_risk_share_pct` 40) are
unchanged by it, and the rows may never be swept for the ceiling that would
have performed best.

UNIT: percent of equity AT RISK (per-name loss-if-stopped / equity), not
notional weight. `share_of_committed_pct` is the share of the committed total
— the quantity the 40% ceiling is actually written in.

UNKNOWN STAYS NULL: `allocation is None` means the held book's risk could not
be read and the allocator never ran, which is not the same fact as a book
carrying no risk, so every measurement is None and `allocator_ran` is 0.
"""
from __future__ import annotations

import json
import math


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _held_risk_pct(existing_pct):
    """The HELD book's at-risk total, separately from what this session
    committed — the ceiling is spent by positions that already exist, and a
    row showing only new grants would read as a near-empty budget.

    `{}` is a book that was read and holds no risk (0.0); None is a book that
    could not be read (None). The two are never collapsed.
    """
    if existing_pct is None:
        return None
    total = 0.0
    for v in dict(existing_pct).values():
        n = _num(v)
        if n is not None:
            total += n
    return round(total, 6)


def shape_risk_budget_row(*, allocation, existing_pct, equity,
                          cluster_share_pct=None) -> dict:
    ran = allocation is not None
    row = {
        "allocator_ran": 1 if ran else 0,
        "equity": _num(equity),
        "cluster_share_pct": _num(cluster_share_pct),
        "held_risk_pct": _held_risk_pct(existing_pct),
        "committed_pct": None,
        "ceiling_pct": None,
        "cluster_shares_json": None,
        "grants_json": None,
        "rationed_names": None,
    }
    if not ran:
        return row
    committed = _num(getattr(allocation, "committed_pct", None))
    row["committed_pct"] = committed
    row["ceiling_pct"] = _num(getattr(allocation, "ceiling_pct", None))
    clusters = []
    for members, pct in (getattr(allocation, "cluster_pct", None) or {}).items():
        at_risk = _num(pct)
        share = None
        if at_risk is not None and committed:
            share = round(at_risk / committed * 100.0, 6)
        clusters.append({
            "members": sorted(str(m) for m in (members or ())),
            "at_risk_pct": None if at_risk is None else round(at_risk, 6),
            "share_of_committed_pct": share,
        })
    clusters.sort(key=lambda r: r["members"])
    row["cluster_shares_json"] = json.dumps(clusters)
    grants = []
    rationed = 0
    for sym, g in sorted((getattr(allocation, "grants", None) or {}).items()):
        limited_by = getattr(g, "limited_by", None)
        if limited_by:
            rationed += 1
        grants.append({
            "symbol": str(sym),
            "requested_pct": _num(getattr(g, "requested_pct", None)),
            "granted_pct": _num(getattr(g, "granted_pct", None)),
            "limited_by": limited_by,
        })
    row["grants_json"] = json.dumps(grants)
    row["rationed_names"] = rationed
    return row
