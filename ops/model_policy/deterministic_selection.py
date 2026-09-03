"""The desk's OWN stated selection rules, written as plain Python and run
against the frozen run-64290730 fixture. **No LLM call anywhere in here.**

Why this exists (docs/WORK.md item 18b, the owner's question): if the rules
the desk already states are complete, they determine what to buy and the
model is not needed for the selection step. If they are not complete, the
gap they leave is where an unspecified preference — "familiarity" — enters,
and the fix is to specify the missing rule rather than to instruct harder.

The rules replayed here, and where each one is stated:

  R1  current technical coverage         `portfolio_manager.md` "What to trade"
  R2  rating is actionable (not neutral) same
  R3  longs must be BUY-eligible         "Deterministic BUY Eligibility" block
  R4  computed R/R >= 1.5, OR a catalyst resolving to a dated Active News
      State Change row naming the symbol (then capped 0.5% risk)
                                         Rule Priority row 7 / Step 5
  R5  net independent source score >= 1  Step 5 agreement ceiling (§9.4);
      `src/risk/rules.py::agreement_ceiling_for_score` refuses net <= 0
  R6  conviction sizing band, capped by  Step 5 "Base RISK allocation"
      max_position_risk_pct and by R5's ceiling

Everything above is a GATE or a CEILING. Nothing above is a RANKING — that
is the finding, and `summarise()` reports it rather than inventing one.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:  # pragma: no cover - import convenience
    sys.path.insert(0, str(_REPO))

from src.agents.portfolio_manager import PortfolioManagerAgent  # noqa: E402
from src.risk.rules import (  # noqa: E402
    agreement_ceiling_for_score,
    count_aligned_sources,
    count_opposing_sources,
    signed_source_score,
)

# Read from config/settings.yaml rather than imported, because `load_config`
# validates API keys this audit does not have and does not need. Pinned by
# `tests/test_deterministic_selection.py` against the YAML so a config edit
# cannot silently desync them.
AGREEMENT_CEILING_PCT = [3.0, 4.0, 5.0, 5.0, 5.0]
MAX_POSITION_RISK_PCT = 5.0
RR_FLOOR = 1.5
SUBFLOOR_CATALYST_RISK_PCT = 0.5
CONVICTION_BANDS = {"high": (1.5, 3.0), "medium": (1.0, 2.0), "low": (0.5, 1.0)}

# `- [2026-08-27] headline → NVDA, SMH, SOXX` — the row shape Rule Priority
# row 7 resolves a sub-floor catalyst against. Only the symbol list after the
# arrow counts; a symbol merely mentioned in the headline prose does not.
_STATE_CHANGE_ROW = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\][^\n]*?→\s*(.+)$", re.M)


def catalyst_symbols(active_state_changes: str) -> set[str]:
    """Symbols a sub-floor pick may legally cite as its catalyst."""
    out: set[str] = set()
    for _date, tail in _STATE_CHANGE_ROW.findall(active_state_changes or ""):
        out.update(part.strip().upper() for part in tail.split(",") if part.strip())
    return out


def evaluate(selection: dict, analyses, positions, news_intel) -> list[dict]:
    """One row per analysed symbol: eligible or the rules that blocked it."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    registry = agent.build_evidence_registry(
        analyses=analyses,
        positions=positions,
        news_intel=news_intel,
        earnings_analyses=selection["earnings_analyses"],
        macro_analysis=selection["macro_analysis"],
        smart_money_findings=[],
        symbol_sectors={},
    )
    stale = agent.stale_evidence_sources(
        earnings_analyses=selection["earnings_analyses"],
    )
    allowed = {
        str(s).strip().upper()
        for s in list(selection["allowed_buy_symbols"])
        + list(selection["transient_admitted_symbols"])
        if str(s).strip()
    }
    catalysts = catalyst_symbols(selection["memory"]["active_state_changes"])
    held = {p.symbol for p in positions}

    rows: list[dict] = []
    for a in analyses:
        symbol = a.symbol
        rr = a.risk_reward
        blocked: list[str] = []
        direction = "short" if a.rating in ("sell", "strong_sell") else "long"

        if a.rating == "neutral":  # R2
            blocked.append("R2 neutral rating")
        if direction == "long" and symbol.upper() not in allowed:  # R3
            blocked.append("R3 not BUY-eligible")

        subfloor_catalyst = False  # R4
        if (rr or 0.0) < RR_FLOOR:
            if symbol.upper() in catalysts:
                subfloor_catalyst = True
            else:
                blocked.append(f"R4 R/R {rr} < {RR_FLOOR}, no dated catalyst row")

        sources = registry.get(symbol, {})  # R5
        ignored = stale.get(symbol)
        net = aligned = opposed = 0
        if sources:
            aligned = count_aligned_sources(
                symbol, sources, direction, ignored_sources=ignored)
            opposed = count_opposing_sources(
                symbol, sources, direction, ignored_sources=ignored)
            net = signed_source_score(
                symbol, sources, direction, ignored_sources=ignored)
        ceiling = agreement_ceiling_for_score(AGREEMENT_CEILING_PCT, net)
        if ceiling <= 0.0:
            blocked.append(f"R5 net evidence {net:+d} — no rung, refused")

        band = CONVICTION_BANDS.get(a.conviction, (0.0, 0.0))  # R6
        max_risk = min(band[1], MAX_POSITION_RISK_PCT, ceiling)
        if subfloor_catalyst:
            max_risk = min(max_risk, SUBFLOOR_CATALYST_RISK_PCT)

        rows.append({
            "symbol": symbol,
            "direction": direction,
            "rating": a.rating,
            "conviction": a.conviction,
            "rr": rr,
            "aligned": aligned,
            "opposed": opposed,
            "net_sources": net,
            "agreement_ceiling_pct": ceiling,
            "max_risk_pct": round(max_risk, 2),
            "subfloor_catalyst": subfloor_catalyst,
            "held": symbol in held,
            "eligible": not blocked,
            "blocked_by": blocked,
        })
    return rows


# --------------------------------------------------------------------------
# The missing RANKING step, at the ratified equal weight. NOT WIRED IN.
# --------------------------------------------------------------------------
# The owner-ratified architecture decision is a weighted composite score
# across signals, **starting equal-weight**. This implements exactly that and
# nothing more: no tuned weights, no new thresholds, no signal that `evaluate`
# does not already compute for every candidate.
#
# Which of `evaluate`'s per-candidate numbers are admissible as SCORE inputs:
#
#   rr                     YES  primitive, continuous
#   net_sources            YES  primitive, integer
#   conviction             YES  ordinal, and the file ALREADY carries the
#                               desk's own numeric encoding of it in
#                               CONVICTION_BANDS (low/medium/high -> 1/2/3
#                               risk-percent band tops). Not a new number.
#   aligned, opposed       NO   net_sources IS aligned - opposed; scoring all
#                               three counts the same evidence three times.
#   agreement_ceiling_pct  NO   a pure function of net_sources (§9.4 lookup);
#                               including it double-weights evidence.
#   max_risk_pct           NO   a function of conviction, the ceiling and the
#                               sub-floor cap — it re-imports the R/R gate the
#                               ranking is supposed to be independent of.
#   subfloor_catalyst,     NO   booleans that restate gate outcomes, not
#   held, eligible               strength-of-candidate signals.
#
# So three independent signals, min-max normalised across the eligible set so
# they share a 0..1 scale, summed at weight 1.0 each. A signal that is
# constant across the set contributes 0 to every candidate (no spurious
# spread), which is the only defensible degenerate case.
RANKING_SIGNALS = ("rr", "net_sources", "conviction_score")

# The desk's own conviction encoding, read off CONVICTION_BANDS rather than
# chosen here, so a config change to the bands moves this with it.
CONVICTION_SCORE = {name: band[1] for name, band in CONVICTION_BANDS.items()}


def _min_max(values: list[float]) -> list[float]:
    """0..1 across the set; an all-equal signal contributes nothing."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank_eligible(rows: list[dict]) -> list[dict]:
    """Equal-weight composite score over the eligible candidates.

    Pure and deterministic. Returns the eligible rows highest score first,
    each with its normalised components and `composite_score`. Ties break on
    symbol so the order is stable. **This is not called by any production
    code path** — see the module docstring and docs/WORK.md item 18.
    """
    eligible = [r for r in rows if r["eligible"]]
    if not eligible:
        return []

    raw = {
        "rr": [float(r["rr"] or 0.0) for r in eligible],
        "net_sources": [float(r["net_sources"]) for r in eligible],
        "conviction_score": [
            float(CONVICTION_SCORE.get(r["conviction"], 0.0)) for r in eligible
        ],
    }
    normalised = {name: _min_max(values) for name, values in raw.items()}

    scored = []
    for i, row in enumerate(eligible):
        components = {name: round(normalised[name][i], 4) for name in RANKING_SIGNALS}
        scored.append({
            **row,
            "score_components": components,
            "composite_score": round(sum(components.values()), 4),
        })
    scored.sort(key=lambda r: (-r["composite_score"], r["symbol"]))
    return scored


def summarise(rows: list[dict]) -> dict:
    eligible = [r for r in rows if r["eligible"]]
    return {
        "analysed": len(rows),
        "eligible": len(eligible),
        "clears_rr_floor_alone": sorted(
            r["symbol"] for r in eligible if not r["subfloor_catalyst"]),
        "enters_via_catalyst_door": sorted(
            r["symbol"] for r in eligible if r["subfloor_catalyst"]),
        "total_max_risk_pct": round(sum(r["max_risk_pct"] for r in eligible), 2),
        # The whole point. A ranking rule would name ONE symbol here.
        "rules_name_a_single_pick": False,
    }


def _main() -> None:  # pragma: no cover - operator entry point
    from ops.model_policy import scenarios as S

    rows = evaluate(
        S._SELECTION, S._SELECTION_ANALYSES, S._SELECTION_POSITIONS, S._SELECTION_NEWS,
    )
    summary = summarise(rows)
    print(f"analysed={summary['analysed']} eligible={summary['eligible']} "
          f"total max risk {summary['total_max_risk_pct']}% "
          f"(budget 25%) -> nothing forces a choice")
    header = (f"{'sym':<7}{'dir':<6}{'rating':<12}{'conv':<8}{'R/R':>6}"
              f"{'net':>5}{'ceil%':>7}{'maxrisk%':>10}  door")
    print(header)
    print("-" * len(header))
    for r in sorted((r for r in rows if r["eligible"]), key=lambda x: -(x["rr"] or 0)):
        door = "catalyst" if r["subfloor_catalyst"] else "R/R floor"
        print(f"{r['symbol']:<7}{r['direction']:<6}{r['rating']:<12}"
              f"{r['conviction']:<8}{(r['rr'] or 0):6.2f}{r['net_sources']:>5}"
              f"{r['agreement_ceiling_pct']:>7.1f}{r['max_risk_pct']:>10.2f}  {door}")

    ranked = rank_eligible(rows)
    print("\nEQUAL-WEIGHT COMPOSITE RANKING (not wired into production):")
    print(f"{'#':<4}{'sym':<7}{'score':>7}   " + "  ".join(
        f"{name:>16}" for name in RANKING_SIGNALS))
    for i, r in enumerate(ranked, 1):
        parts = "  ".join(f"{r['score_components'][n]:>16.4f}" for n in RANKING_SIGNALS)
        print(f"{i:<4}{r['symbol']:<7}{r['composite_score']:>7.4f}   {parts}")
    print("order: " + ", ".join(r["symbol"] for r in ranked))


if __name__ == "__main__":  # pragma: no cover
    _main()
