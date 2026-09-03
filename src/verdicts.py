"""Phase 13 — ranking analyst verdicts, at a research-informed prior weight.

`docs/QAMC_REMEDIATION_SPEC.md` §13: the Portfolio Manager's rules gate and
ceiling but never ORDER. Read as code on run-64290730 (item 18, 2026-09-03)
they took 59 analysed names to 12 eligible and stopped — no tiebreaker, so
the effective choice fell to whatever the model defaulted toward. This module
is the missing ordering step, over the shared `AnalystVerdict` shape.

Pure logic only: no I/O, no LLM calls, no broker access — same posture and
same reason as `src/nominations.py`. Which candidates are ELIGIBLE is not
decided here; `PortfolioManagerAgent.candidate_eligibility` applies the
desk's gates, and this module orders only what it is handed.

**The score.** Two signals a verdict carries, both already on a 0..1 scale:

  * `magnitude`        — how far the seat leans (`AnalystVerdict.magnitude`).
  * `conviction_score` — the seat's declared conviction, low/medium/high
                         placed at 0 / 0.5 / 1. Equal spacing: it is the same
                         ordinal the `_CONVICTION_RANK` in `src/nominations.py`
                         and `CONVICTION_SCORE` in the item-18 audit script
                         already use, rescaled to share the magnitude's range.

No min-max normalisation across the candidate set: both inputs are absolute,
so a candidate's score does not move when a different peer joins the list —
which is what makes the order reproducible from the verdicts alone.

**§13.3 AMENDED 2026-09-03 (owner decision, this ranking module only —
`src/risk/rules.py::SEAT_WEIGHT`, which sizes real positions, is untouched
and still requires 20+ of this desk's own resolved calls per seat before any
weight applies there).** The original rule was "start equal, adjust only on
this desk's own out-of-sample proof" — sound in principle, but the owner's
point is that "equal until we have our own data" is functionally identical
to "always equal," for however long that data takes to accumulate, which
directly contradicts the desk's own repeated, load-bearing instruction that
analysts are never interchangeable. The amendment: use a real, externally
published prior now, on outside literature about the general reliability of
each TYPE of analysis (not a claim about how good THIS desk's specific
analyst prompts are) — and let it be overridden the moment the desk's own
conviction ledger clears the 20-resolved-call bar per seat
(`_CONVICTION_OUTCOME_MIN_N`, `src/storage/db.py`; not yet wired — tracked in
`docs/WORK.md`).

`SEAT_WEIGHT` is a per-seat multiplier, sourced from real published research
(five parallel literature reviews, 2026-09-03, WebSearch-verified — see
`docs/QAMC_REMEDIATION_SPEC.md` §13.3 for the full citation list per seat):

  * **technical 1.2, earnings 1.2** — the two strongest, most-replicated
    effects found: cross-sectional momentum (Jegadeesh & Titman 1993,
    replicated internationally by Rouwenhorst 1998) and post-earnings-
    announcement drift (Ball & Brown 1968; Bernard & Thomas 1989/1990) are
    among the most persistent anomalies in the academic literature, though
    both are documented to be decaying over time as more capital trades on
    them — this is a real edge, not a permanent one.
  * **news 1.0** — a genuine, replicated effect (Tetlock 2007) but narrow:
    short-horizon (days), concentrated in small/illiquid names, and the
    literature specifically flags LLM-generated sentiment for looking strong
    in-sample and failing out-of-sample (Benhenda 2026) — directly relevant
    to this seat's own nature. Left at the unweighted baseline rather than
    above it.
  * **smart_money 0.8, macro 0.8** — both carry a real effect in the
    literature that is specifically undermined by how this desk actually
    consumes the signal: smart-money/insider edges live almost entirely in
    the pre-disclosure window and are largely gone by the time a filing is
    public (Cohen, Malloy & Pomorski 2012; a 2025 Finance Research Letters
    lag study) — all this desk ever sees is the public, lagged version.
    Macro's "regime matters" claim is well supported, but the specific
    capability this seat is asked for — calling turning points — is one of
    the most consistently, repeatedly debunked findings in applied
    macroeconomics (IMF WP/18/39 studied 153 recession episodes across 63
    countries and found professional forecasters missed the onset in the
    vast majority of them).

These are modest, deliberately bounded multipliers (not aggressive ones) —
no source in the research handed over an exact cross-category ratio, only a
real, sourced ordinal ranking of confidence. Treat the specific numbers as a
considered but revisable starting point, not a measured fact.

When more than one seat has a verdict on the same symbol (not yet — only
Technical produces one in this increment, so this weighting has **no
observable effect on today's ranking** until a second seat is wired in —
tracked in `docs/WORK.md`), the per-seat scores are averaged AT THIS WEIGHT,
so two agreeing seats of different trustworthiness are not treated as
interchangeable votes.

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
    "seat_weight",
    "rank_verdicts",
]

#: Ordinal encoding of the desk's own conviction scale onto 0..1. Equal
#: spacing — nothing measured justifies anything else.
CONVICTION_SCORE: dict[str, float] = {"low": 0.0, "medium": 0.5, "high": 1.0}

#: The two verdict fields the score reads, each at weight 1.0.
RANKING_SIGNALS: tuple[str, ...] = ("magnitude", "conviction_score")

#: Research-informed prior, per seat. See the module docstring for the
#: citations behind each number and why this module (ranking only, no
#: sizing impact) is allowed one while `src/risk/rules.py::SEAT_WEIGHT`
#: (sizing) is not.
SEAT_WEIGHT: dict[str, float] = {
    "technical": 1.2,
    "earnings": 1.2,
    "news": 1.0,
    "smart_money": 0.8,
    "macro": 0.8,
}

#: Fallback for a seat this table doesn't name (a future seat added to the
#: Phase 13 shape before its own literature review lands) — unweighted,
#: never zero, so an unreviewed seat is never silently muted.
_DEFAULT_SEAT_WEIGHT = 1.0


def seat_weight(seat: str) -> float:
    return SEAT_WEIGHT.get(seat, _DEFAULT_SEAT_WEIGHT)


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

    **One verdict per (symbol, seat).** A caller CAN hand this two verdicts
    from the same seat on the same symbol in one run — e.g. earnings, if two
    filings for one ticker were both analysed the same session (a real,
    if uncommon, case: nothing upstream enforces one-filing-per-symbol-per-
    run). Averaging every seat's contribution already assumes one vote per
    seat (`SEAT_WEIGHT` is keyed by seat name, not by verdict) — silently
    letting a duplicate through would double that seat's weight without
    anyone deciding it should count twice. Found and fixed 2026-09-03 by an
    adversarial review before this shipped: caught via the concrete case of
    two earnings verdicts pushing earnings' effective weight from 50% to
    66.7% of a two-seat group. LAST ONE WINS per (symbol, seat), input
    order — the same "last-wins per symbol" convention already used for
    the evidence registry (`PortfolioManagerAgent._earnings_stance_rows`),
    applied one level finer (per seat, not just per symbol).
    """
    by_symbol: dict[str, dict[str, AnalystVerdict]] = {}
    for verdict in verdicts:
        if verdict.direction == "neutral":
            continue
        by_symbol.setdefault(verdict.symbol.upper(), {})[verdict.seat] = verdict

    ranked: list[RankedCandidate] = []
    for symbol, seat_verdicts in by_symbol.items():
        group = list(seat_verdicts.values())
        directions = {v.direction for v in group}
        if len(directions) != 1:
            continue
        weights = [seat_weight(v.seat) for v in group]
        total_weight = sum(weights)
        magnitude = sum(v.magnitude * w for v, w in zip(group, weights)) / total_weight
        conviction = sum(
            conviction_score(v.conviction) * w for v, w in zip(group, weights)
        ) / total_weight
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
