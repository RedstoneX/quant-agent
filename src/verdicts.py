"""Phase 13 — ranking analyst verdicts, at the ratified equal weight.

`docs/QAMC_REMEDIATION_SPEC.md` §13: the Portfolio Manager's rules gate and
ceiling but never ORDER. Read as code on run-64290730 (item 18, 2026-09-03)
they took 59 analysed names to 12 eligible and stopped — no tiebreaker, so
the effective choice fell to whatever the model defaulted toward. This module
is the missing ordering step, over the shared `AnalystVerdict` shape.

Pure logic only: no I/O, no LLM calls, no broker access — same posture and
same reason as `src/nominations.py`. Which candidates are ELIGIBLE is not
decided here; `PortfolioManagerAgent.candidate_eligibility` applies the
desk's gates, and this module orders only what it is handed.

**The score.** Two signals a verdict carries, both already on a 0..1 scale,
summed at weight 1.0 each:

  * `magnitude`        — how far the seat leans (`AnalystVerdict.magnitude`).
  * `conviction_score` — the seat's declared conviction, low/medium/high
                         placed at 0 / 0.5 / 1. Equal spacing: it is the same
                         ordinal the `_CONVICTION_RANK` in `src/nominations.py`
                         and `CONVICTION_SCORE` in the item-18 audit script
                         already use, rescaled to share the magnitude's range.

No min-max normalisation across the candidate set: both inputs are absolute,
so a candidate's score does not move when a different peer joins the list —
which is what makes the order reproducible from the verdicts alone.

**Equal weight is not a placeholder for a better number** (§13.3): each
seat's contribution is to be weighted by ITS OWN measured reliability for the
kind of judgement it is making, and no such measurement exists yet — the
book has too few resolved calls (`_CONVICTION_OUTCOME_MIN_N` in
`src/storage/db.py`). `SEAT_WEIGHT` here is pinned at one for the same reason
`src/risk/rules.py::SEAT_WEIGHT` is, and `tests/test_analyst_verdict.py`
fails if it becomes a table. When more than one seat has a verdict on the
same symbol (not yet — only Technical produces one in this increment), the
per-seat scores are AVERAGED at unit weight, so a name covered by two seats
is not double-counted against a name covered by one.

Ties break on symbol, so the order is stable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models import AnalystVerdict

__all__ = [
    "CONVICTION_SCORE",
    "RANKING_SIGNALS",
    "SEAT_WEIGHT",
    "RankedCandidate",
    "conviction_score",
    "score_verdict",
    "rank_verdicts",
]

#: Ordinal encoding of the desk's own conviction scale onto 0..1. Equal
#: spacing — nothing measured justifies anything else.
CONVICTION_SCORE: dict[str, float] = {"low": 0.0, "medium": 0.5, "high": 1.0}

#: The two verdict fields the score reads, each at weight 1.0.
RANKING_SIGNALS: tuple[str, ...] = ("magnitude", "conviction_score")

#: Every seat's verdict enters at unit weight. Not a table, on purpose —
#: see the module docstring and `src/risk/rules.py::SEAT_WEIGHT`.
SEAT_WEIGHT: int = 1


def conviction_score(conviction: str) -> float:
    return CONVICTION_SCORE.get(conviction, 0.0)


def score_verdict(verdict: AnalystVerdict) -> float:
    """One verdict's composite: magnitude + conviction_score, weight 1 each."""
    return round(verdict.magnitude + conviction_score(verdict.conviction), 4)


@dataclass
class RankedCandidate:
    """One symbol's place in the order, with the arithmetic that put it there."""

    symbol: str
    direction: str
    score: float
    verdicts: list[AnalystVerdict] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)

    @property
    def seats(self) -> list[str]:
        return [v.seat for v in self.verdicts]


def rank_verdicts(verdicts: list[AnalystVerdict]) -> list[RankedCandidate]:
    """Order candidates highest composite first; ties on symbol.

    Neutral verdicts are not candidates for anything and are skipped. A
    symbol whose seats disagree on direction is NOT ranked here — that is
    a conflict for §9.3/§9.4 to adjudicate, and ordering it would hide the
    disagreement inside a number. It is dropped with the reason recorded on
    the caller's side (`candidate_eligibility`), never silently.
    """
    by_symbol: dict[str, list[AnalystVerdict]] = {}
    for verdict in verdicts:
        if verdict.direction == "neutral":
            continue
        by_symbol.setdefault(verdict.symbol.upper(), []).append(verdict)

    ranked: list[RankedCandidate] = []
    for symbol, group in by_symbol.items():
        directions = {v.direction for v in group}
        if len(directions) != 1:
            continue
        n = len(group)
        magnitude = sum(v.magnitude for v in group) / n
        conviction = sum(conviction_score(v.conviction) for v in group) / n
        components = {
            "magnitude": round(magnitude, 4),
            "conviction_score": round(conviction, 4),
        }
        ranked.append(RankedCandidate(
            symbol=symbol,
            direction=directions.pop(),
            score=round(sum(components.values()), 4),
            verdicts=sorted(group, key=lambda v: v.seat),
            components=components,
        ))
    ranked.sort(key=lambda c: (-c.score, c.symbol))
    return ranked
