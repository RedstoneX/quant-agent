"""Phase 9 §9.5 — the conviction ledger: who took which side, and what it cost.

`docs/QAMC_REMEDIATION_SPEC.md` §9.5 asks for "who nominated it, who
confirmed, who dissented and on what grounds". §9.1/§9.2 already record the
first half: every raw nomination is persisted as a `pipeline_event` evidence
row carrying seat, conviction and observation. What was missing was the other
three quarters — the JOIN from a nomination to the decision and trade it
became, an explicit record of the seats that argued the OTHER way, and any
scoring of either against a realized outcome.

This module is the pure-logic half of that, and pure logic ONLY: no I/O, no
DB handle, no LLM calls, no broker access. Same posture and same reason as
`src/nominations.py` — the scoring and aggregation rules are the part worth
testing, and they should be testable without constructing a `Database` or a
`TradingPipeline`. The persistence side lives on `Database`
(`record_seat_stances`, `resolve_conviction_ledger`, `get_conviction_credits`).

**Advisory only.** Nothing here is read by the trading decision chain. No
function in this module is called from sizing, risk allocation, or order
construction, and none of them may become so without a separate ratified
decision — the spec is explicit that the ledger exists to make the desk
legible to the operator, not to change what the desk trades.

Three ideas:

**A stance is a side taken.** A seat that rated a symbol bullish while the
desk went long SUPPORTED the trade; one that rated it bearish OPPOSED it. A
neutral seat took no side and is not scored — crediting "no view" either way
would let a seat accumulate a record by abstaining. Alignment is decided by
`src/risk/rules.py::stance_is_aligned`, the same vocabulary the §9.4
agreement ceiling and `validate_grounding` already share; a second, divergent
notion of "aligned" here would let the ledger and the sizing gate disagree
about identical evidence.

**Credit is signed R, weighted by declared conviction.** A supporter of a
trade that made +2R scores +2R; an opposer of that same trade scores -2R. If
the trade had instead lost 1R, the opposer would score +1R — being right to
argue against a loser is the whole point of recording dissent. `r_multiple`
(`src/risk/metrics.py`) is the measure, unchanged: profit in units of the
risk originally taken, against the stop the position was OPENED with.

**No thresholds.** `aggregate_seat_records` returns raw counts and raw
averages with no minimum-sample gate, deliberately. Owner decision: return
the numbers and let the reader judge whether four resolved calls mean
anything, rather than hiding them behind a threshold this desk has no
evidence for.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from src.risk.rules import stance_is_aligned

__all__ = [
    "CONVICTION_WEIGHT",
    "DEFAULT_CONVICTION",
    "SeatStance",
    "SeatCredit",
    "SeatRecord",
    "ClosedPosition",
    "normalize_seat",
    "conviction_weight",
    "score_position",
    "aggregate_seat_records",
    "summarize_closed_position",
]


#: Weight applied to a seat's R when it declared this conviction. A seat that
#: said "high" and was wrong loses more than one that hedged at "low", which
#: is the only way a self-declared confidence number ever becomes falsifiable.
#: These numbers are a starting calibration, not a measured result — every
#: credit row also persists the unweighted `r_multiple` and the declared
#: conviction, so a reader who disagrees with this scale can re-weight the
#: whole ledger from stored data without any recomputation of outcomes.
CONVICTION_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}

#: Used when a seat took a side but declared no conviction — every seat that
#: did not NOMINATE the symbol is in this case, since a stance in the evidence
#: registry carries a direction but no strength. Deliberately the middle
#: weight: neither rewarding nor punishing a seat for the schema's silence.
DEFAULT_CONVICTION = "medium"

#: Nomination seat names (`_collect_seat_nominations`) → evidence-registry
#: source names (`PortfolioManagerAgent.build_evidence_registry`). The same
#: desk seat is called `news_analyst` on the nomination side and `news` on the
#: registry side; the ledger has to hold both against one identity or a seat's
#: nominations and its stances score as two different analysts.
_SEAT_ALIASES: dict[str, str] = {
    "news_analyst": "news",
    "macro_analyst": "macro",
    "earnings_analyst": "earnings",
    "tech_analyst": "technical",
    "technical_analyst": "technical",
    "smart_money_analyst": "smart_money",
}


def normalize_seat(seat: str) -> str:
    """One canonical name per desk seat, whichever side of the pipeline
    supplied it. Unknown names pass through lowercased rather than being
    dropped — a new seat should appear in the ledger under its own name, not
    vanish from it."""
    key = str(seat or "").strip().lower()
    return _SEAT_ALIASES.get(key, key)


def conviction_weight(conviction: str | None) -> float:
    """Weight for a declared conviction; the medium weight when absent or
    unrecognized. Never zero — a seat that took a side is always scored."""
    key = str(conviction or "").strip().lower()
    return CONVICTION_WEIGHT.get(key, CONVICTION_WEIGHT[DEFAULT_CONVICTION])


@dataclass(frozen=True)
class SeatStance:
    """One seat's recorded side on one idea, at the moment the desk decided.

    `stance` is the canonical evidence-registry stance ("buy", "bearish",
    "underweight", ...). `conviction` is what the seat DECLARED when it
    nominated the symbol, or `DEFAULT_CONVICTION` when it only rated a symbol
    someone else raised. `nominated` distinguishes "asked the desk to look at
    this" from "was asked about it and answered" — §9.5 wants both, and they
    are not the same claim.
    """

    seat: str
    symbol: str
    stance: str
    conviction: str = DEFAULT_CONVICTION
    nominated: bool = False
    observation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat", normalize_seat(self.seat))
        object.__setattr__(self, "symbol", str(self.symbol or "").strip().upper())
        object.__setattr__(self, "stance", str(self.stance or "").strip().lower())


@dataclass(frozen=True)
class SeatCredit:
    """One seat's scored outcome on one resolved idea.

    `credit` is the signed, conviction-weighted score. `r_multiple` is the
    unweighted realized R of the trade itself (identical across every seat on
    the same position) and `weight` the multiplier applied, both kept so the
    scale can be revisited later without re-deriving outcomes.
    """

    seat: str
    symbol: str
    side: str            # "supported" | "opposed"
    stance: str
    conviction: str
    weight: float
    r_multiple: float
    credit: float
    resolved_at: str = ""
    position_id: str | None = None
    decision_id: str | None = None
    direction: str = "long"
    nominated: bool = False


@dataclass(frozen=True)
class SeatRecord:
    """One seat's aggregate record. Raw numbers, no gate — see module docstring.

    `cumulative` is the seat's running credit total after each resolved call,
    oldest first, as (resolved_at, cumulative_credit) pairs — the profit
    series §9.5 asks for. `peak` is the highest that series ever reached
    INCLUDING the zero it starts from, so a seat whose every call lost money
    is in drawdown from 0.0 rather than from its first (negative) point.
    """

    seat: str
    resolved_calls: int
    calls_right: int
    avg_win: float | None
    avg_loss: float | None
    cumulative_credit: float
    cumulative: list[tuple[str, float]] = field(default_factory=list)
    peak: float = 0.0
    current_drawdown: float = 0.0

    @property
    def win_rate_pct(self) -> float | None:
        """Share of resolved calls that scored positive, or None with no calls.
        A zero-R call (the trade closed exactly at entry) counts as resolved
        and not-right, which is the honest reading: the seat's side made no
        money."""
        if self.resolved_calls <= 0:
            return None
        return round(self.calls_right / self.resolved_calls * 100, 2)


@dataclass(frozen=True)
class ClosedPosition:
    """A position chain that has gone flat, reduced to what scoring needs.

    Produced by `summarize_closed_position` from one `position_id`'s trades
    rows. `initial_stop` is the stop the position was OPENED with — the
    R-multiple denominator — and is None when no entry row recorded one, in
    which case the position is not scorable and is skipped rather than
    scored against a fabricated risk.
    """

    position_id: str
    symbol: str
    direction: str
    decision_id: str | None
    entry_price: float
    exit_price: float
    initial_stop: float | None
    qty: float
    opened_at: str
    closed_at: str


def score_position(
    *,
    symbol: str,
    direction: str,
    r_multiple: float,
    stances: Iterable[SeatStance],
    position_id: str | None = None,
    decision_id: str | None = None,
    resolved_at: str = "",
) -> list[SeatCredit]:
    """Credit every seat that took a side on `symbol`, given the realized R.

    Supporters of the direction actually taken score `+weight * r_multiple`;
    opposers score `-weight * r_multiple`. A trade that lost therefore pays
    its dissenters, which is the asymmetry §9.5 exists to capture.

    Seats whose stance is neither bullish nor bearish (`neutral`, an empty
    string, an unrecognized word) took no side and produce no credit row.
    Deterministic: output is sorted by (seat, symbol) so two identical inputs
    in a different order persist identical rows.
    """
    wants_bullish = str(direction or "long").strip().lower() != "short"
    out: list[SeatCredit] = []
    for stance in stances:
        if stance.symbol and stance.symbol != str(symbol).strip().upper():
            continue
        supported = stance_is_aligned(
            stance.seat, symbol, stance.stance, wants_bullish=wants_bullish,
        )
        opposed = stance_is_aligned(
            stance.seat, symbol, stance.stance, wants_bullish=not wants_bullish,
        )
        if supported == opposed:
            # Neither (neutral / unknown stance) or — impossible with the
            # current disjoint vocabularies — both. No side taken, no credit.
            continue
        weight = conviction_weight(stance.conviction)
        signed_r = r_multiple if supported else -r_multiple
        out.append(SeatCredit(
            seat=stance.seat,
            symbol=str(symbol).strip().upper(),
            side="supported" if supported else "opposed",
            stance=stance.stance,
            conviction=str(stance.conviction or DEFAULT_CONVICTION).strip().lower(),
            weight=weight,
            r_multiple=round(float(r_multiple), 4),
            credit=round(weight * signed_r, 4),
            resolved_at=resolved_at,
            position_id=position_id,
            decision_id=decision_id,
            direction="short" if not wants_bullish else "long",
            nominated=bool(stance.nominated),
        ))
    return sorted(out, key=lambda c: (c.seat, c.symbol))


def aggregate_seat_records(credits: Iterable[SeatCredit]) -> dict[str, SeatRecord]:
    """Per-seat record over resolved calls: counts, averages, profit series,
    drawdown from the seat's own peak.

    Pure: takes already-scored credits and returns arithmetic. No I/O, no
    thresholds, no minimum sample size — `resolved_calls` is returned raw so
    the reader can judge whether the rest of the row means anything.

    Ordering of the cumulative series is by (`resolved_at`, `position_id`,
    `symbol`), so it is stable and reproducible regardless of the order rows
    came back from storage. `avg_loss` is returned NEGATIVE (the mean of the
    losing credits as they were scored), not as a magnitude.
    """
    by_seat: dict[str, list[SeatCredit]] = {}
    for credit in credits:
        by_seat.setdefault(credit.seat, []).append(credit)

    records: dict[str, SeatRecord] = {}
    for seat, rows in by_seat.items():
        ordered = sorted(
            rows,
            key=lambda c: (c.resolved_at or "", c.position_id or "", c.symbol),
        )
        wins = [c.credit for c in ordered if c.credit > 0]
        losses = [c.credit for c in ordered if c.credit < 0]
        running = 0.0
        peak = 0.0
        series: list[tuple[str, float]] = []
        for credit in ordered:
            running += credit.credit
            series.append((credit.resolved_at or "", round(running, 4)))
            peak = max(peak, running)
        records[seat] = SeatRecord(
            seat=seat,
            resolved_calls=len(ordered),
            calls_right=len(wins),
            avg_win=round(sum(wins) / len(wins), 4) if wins else None,
            avg_loss=round(sum(losses) / len(losses), 4) if losses else None,
            cumulative_credit=round(running, 4),
            cumulative=series,
            peak=round(peak, 4),
            current_drawdown=round(max(0.0, peak - running), 4),
        )
    return records


def _executed_qty(row: dict) -> float:
    """Shares this row actually moved. Prefers the reconciled fill over the
    requested quantity, mirroring `compute_trade_calibration`."""
    try:
        fill = float(row.get("fill_qty") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill > 0:
        return fill
    try:
        return float(row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _executed_price(row: dict) -> float:
    try:
        fill = float(row.get("fill_price") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill > 0:
        return fill
    try:
        return float(row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


#: Opening actions, mapped to the direction the position carries. SHORT is
#: listed because the ledger's arithmetic is direction-aware — NOT because
#: short chains exist today: `_assign_position_ids` (src/storage/db.py) only
#: opens a chain on a BUY, so no SHORT currently receives a position_id at
#: all. That is a pre-existing gap in §6.2a's chaining, not something this
#: module introduces or repairs; when it is closed, this side already works.
_OPEN_ACTIONS: dict[str, str] = {"BUY": "long", "SHORT": "short"}


def summarize_closed_position(rows: list[dict]) -> ClosedPosition | None:
    """Reduce one position chain's trades rows to a scorable round trip.

    `rows` are every `trades` row sharing one `position_id`, oldest first,
    each a dict with at least symbol/action/qty/price/fill_qty/fill_price/
    fill_status/stop_loss/decision_id/timestamp.

    Returns None — never a guess — when the chain is not scorable:
      - it never opened (no BUY/SHORT row),
      - it is still open (executed exit quantity has not retired the entries),
      - entry or exit price is missing or non-positive,
      - or the exit rows moved no shares (a placed-but-never-filled
        TRAIL_STOP is protection sitting there, not an exit — the same
        distinction `_is_filled_trail_stop` already draws).

    A chain with no `stop_loss` on any entry row IS returned, with
    `initial_stop=None`; the caller skips it rather than inventing a
    denominator, and the distinction is worth preserving for the caller's
    own reporting.
    """
    opens: list[dict] = []
    exits: list[dict] = []
    direction: str | None = None
    for row in rows:
        action = str(row.get("action") or "").upper()
        if action in _OPEN_ACTIONS:
            if direction is None:
                direction = _OPEN_ACTIONS[action]
            opens.append(row)
        elif action == "TRAIL_STOP":
            # Only a FILLED trail stop closed anything.
            status = str(row.get("fill_status") or "").lower()
            filled = status == "filled" or (status == "" and _executed_qty(row) > 0)
            if filled and _executed_qty(row) > 0:
                exits.append(row)
        elif action:
            exits.append(row)

    if not opens or direction is None or not exits:
        return None

    open_qty = sum(_executed_qty(r) for r in opens)
    exit_qty = sum(_executed_qty(r) for r in exits)
    if open_qty <= 0 or exit_qty <= 0:
        return None
    if exit_qty + 1e-6 < open_qty:
        return None  # still open — outcome is not known yet

    def _vwap(chain: list[dict]) -> float:
        total_qty = sum(_executed_qty(r) for r in chain)
        if total_qty <= 0:
            return 0.0
        return sum(_executed_qty(r) * _executed_price(r) for r in chain) / total_qty

    entry_price = _vwap(opens)
    exit_price = _vwap(exits)
    if entry_price <= 0 or exit_price <= 0:
        return None

    initial_stop: float | None = None
    for row in opens:
        try:
            stop = float(row.get("stop_loss") or 0)
        except (TypeError, ValueError):
            stop = 0.0
        if stop > 0:
            initial_stop = stop
            break

    decision_id = next(
        (row.get("decision_id") for row in opens if row.get("decision_id")), None,
    )
    position_id = next(
        (row.get("position_id") for row in rows if row.get("position_id")), "",
    )
    return ClosedPosition(
        position_id=str(position_id or ""),
        symbol=str(opens[0].get("symbol") or "").strip().upper(),
        direction=direction,
        decision_id=decision_id,
        entry_price=entry_price,
        exit_price=exit_price,
        initial_stop=initial_stop,
        qty=open_qty if direction == "long" else -open_qty,
        opened_at=str(opens[0].get("timestamp") or ""),
        closed_at=str(exits[-1].get("timestamp") or ""),
    )
