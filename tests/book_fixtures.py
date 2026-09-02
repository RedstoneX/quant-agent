"""Adversarial book fixtures — portfolios built so that a NAIVE and a CORRECT
implementation of the same quantity return DIFFERENT numbers.

Why this module exists
----------------------
On 2026-09-01 six real defects were measured in a codebase with 4,110 passing
tests. Every one of them was a *seam* defect: two components each computed the
same named quantity, each was tested against itself, and nothing ever asserted
that the two AGREED. The reason 4,110 tests missed them is visible in
`ordinary_long_book()` below — on a long-only, unlevered, all-winning book,
every single one of those defects returns the identical number from the naive
and the correct implementation. The bug is invisible by construction.

So these fixtures are deliberately hostile. Each one isolates ONE property that
separates a naive implementation from a correct one:

    leveraged_inverse_book()   leverage multiplier  (raw vs gross vs net)
    net_short_book()           sign                 (equity - cash goes negative)
    winning_short_book()       sign of cost basis   (a `> 0` guard swallows it)
    sweep_parked_book()        what counts as cash  (parked sweep vehicle)
    halted_session_bars()      window + zero bars   (20 vs 21, dropped vs kept)

They are plain data with no I/O and no config dependency, so anything may
import them. Prefer extending a fixture here over hand-rolling a book inside a
test — a book that lives here gets exercised by the cross-consistency suite and
therefore keeps earning its keep.

Every number below is chosen so the divergence is exactly computable by hand;
the docstring on each fixture states the arithmetic. If you change a number,
change the docstring, or the next reader will trust a stale claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.models import OHLCV, Position

# The cash-sweep vehicle. Held as a position but is parked CASH, not exposure.
SWEEP_SYMBOL = "SGOV"

# `src/risk/rules.py::_ETF_LEVERAGE` — SQQQ is -3x Nasdaq 100. Held LONG, it is
# a bearish position consuming 3x its own market value in gross notional.
INVERSE_3X_SYMBOL = "SQQQ"


@dataclass(frozen=True)
class Book:
    """A portfolio snapshot plus the account scalars every consumer needs.

    `total_value` is equity (Alpaca `portfolio_value`), NOT the sum of market
    values — for a book holding shorts those differ, and conflating them is
    itself one of the defects this module exists to expose.
    """

    name: str
    positions: list[Position]
    cash: float
    total_value: float
    #: What this fixture is designed to break, in one line.
    trap: str

    def by_symbol(self, symbol: str) -> Position:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        raise KeyError(f"{symbol} not in {self.name}")


def _pos(
    symbol: str,
    qty: float,
    avg_entry: float,
    current_price: float,
    sector: str = "Technology",
) -> Position:
    """Build a Position with market_value and unrealized_pnl kept CONSISTENT.

    Both are derived from qty x price rather than passed in, so a fixture can
    never accidentally encode an impossible book (a position whose stated P&L
    disagrees with its own entry and mark). For a short, qty is negative, so
    market_value is negative and a favourable move produces a POSITIVE pnl —
    which is precisely the case that breaks `cost_basis > 0`.
    """
    market_value = qty * current_price
    unrealized_pnl = qty * (current_price - avg_entry)
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        sector=sector,
    )


# ---------------------------------------------------------------------------
# The control. Every defect in the 2026-09-01 survey is INVISIBLE here.
# ---------------------------------------------------------------------------

def ordinary_long_book() -> Book:
    """Long-only, unlevered, no sweep, all winners — the book the suite had.

    raw invested  = 100_000 - 40_000        = 60_000  -> 60.0%
    gross         = 20_000x1 + 40_000x1     = 60_000  -> 60.0%
    net           = 20_000x1 + 40_000x1     = 60_000  -> 60.0%

    All three agree. So does every position weight (multiplier is 1.0), and
    every cost basis is positive so the `> 0` guard never fires. A test written
    against this book cannot distinguish a correct implementation from any of
    the six broken ones. That is not a hypothetical — it is what shipped.
    """
    return Book(
        name="ordinary_long",
        positions=[
            _pos("AAPL", 100, 150.0, 200.0, "Technology"),
            _pos("JNJ", 400, 90.0, 100.0, "Healthcare"),
        ],
        cash=40_000.0,
        total_value=100_000.0,
        trap="none — this is the control that hides every defect",
    )


# ---------------------------------------------------------------------------
# Trap 1 — the leverage multiplier.
# ---------------------------------------------------------------------------

def leveraged_inverse_book() -> Book:
    """A 3x inverse ETF held long, alongside an ordinary long.

    positions   AAPL  mv +20_000  multiplier +1.0
                SQQQ  mv +40_000  multiplier -3.0  (gross 3.0)
    cash        40_000        equity 100_000

    raw invested   = (100_000 - 40_000) / 100_000            =  60.0%
    gross exposure = (|20_000x1| + |40_000x3|) / 100_000      = 140.0%
    net exposure   = |20_000x1 + 40_000x-3| / 100_000         = 100.0%

    Three different answers to "how invested is the book", 80pp apart, from one
    portfolio. The PM is told 60% and the RM is told 100%, and both are judged
    against the same target.

    SQQQ's own weight: raw 40.0% vs gross 120.0%. A 12% drift threshold is
    crossed by one and not the other.
    """
    return Book(
        name="leveraged_inverse",
        positions=[
            _pos("AAPL", 100, 150.0, 200.0, "Technology"),
            _pos(INVERSE_3X_SYMBOL, 500, 90.0, 80.0, "Index"),
        ],
        cash=40_000.0,
        total_value=100_000.0,
        trap="raw / gross / net exposure differ by up to 80pp",
    )


# ---------------------------------------------------------------------------
# Trap 2 — sign. A net-short book makes `equity - cash` go NEGATIVE.
# ---------------------------------------------------------------------------

def net_short_book() -> Book:
    """Net short: short proceeds inflate cash ABOVE equity.

    positions   TSLA  qty -200 @ 250 -> 200   mv -40_000
    cash        130_000       equity  90_000   (= 130_000 - 40_000)

    raw invested = (90_000 - 130_000) / 90_000 = -44.4%   <- negative nonsense
    net exposure = |-40_000| / 90_000          = +44.4%

    The naive form does not merely differ, it changes SIGN. A PM told the book
    is -44% invested against a 70% target reads it as catastrophically
    under-deployed and buys into an already-short book.
    """
    return Book(
        name="net_short",
        positions=[
            _pos("TSLA", -200, 250.0, 200.0, "Consumer Cyclical"),
        ],
        cash=130_000.0,
        total_value=90_000.0,
        trap="equity - cash is NEGATIVE; sign flips against the correct answer",
    )


# ---------------------------------------------------------------------------
# Trap 3 — a winning short, where cost basis is negative.
# ---------------------------------------------------------------------------

def winning_short_book() -> Book:
    """A short that is UP, held next to a long that is up by the same dollars.

    TSLA  qty -200  entry 250 -> 200   pnl  +10_000   cost basis  -50_000
    AAPL  qty +100  entry 150 -> 250   pnl  +10_000   cost basis  +15_000

    naive  cost_basis = avg_entry * qty, then `if cost_basis > 0`
           TSLA -> -50_000, guard fails, pnl_pct = 0.0
           renders "+$10000.00 (+0.0%)" — a winner reported as flat.
    correct cost_basis = abs(avg_entry * qty)
           TSLA -> 50_000, pnl_pct = +20.0%

    The two positions made identical dollars, so any implementation that
    reports different percentages for a reason OTHER than basis size is wrong.
    """
    return Book(
        name="winning_short",
        positions=[
            _pos("TSLA", -200, 250.0, 200.0, "Consumer Cyclical"),
            _pos("AAPL", 100, 150.0, 250.0, "Technology"),
        ],
        cash=80_000.0,
        total_value=100_000.0,
        trap="negative cost basis is swallowed by a `> 0` guard",
    )


# ---------------------------------------------------------------------------
# Trap 4 — parked cash-sweep holdings.
# ---------------------------------------------------------------------------

def sweep_parked_book() -> Book:
    """Part of the cash is parked in the sweep vehicle, held as a position.

    AAPL  mv 46_000        real exposure
    SGOV  mv 14_000        parked CASH wearing a position's clothes
    cash  40_000           equity 100_000

    engine `_compute_deployable_cash` = cash + parked   = 54_000
    API    max(cash - reserve, 0), reserve 1% of equity = 39_000

    A $15,000 disagreement about how much money exists. The engine sizes trades
    against one number while the operator's dashboard shows the other.

    SGOV must ALSO be excluded from exposure: counting parked cash as invested
    consumes the gross allowance while doing nothing.
    """
    return Book(
        name="sweep_parked",
        positions=[
            _pos("AAPL", 230, 150.0, 200.0, "Technology"),
            _pos(SWEEP_SYMBOL, 140, 100.0, 100.0, "Cash Equivalent"),
        ],
        cash=40_000.0,
        total_value=100_000.0,
        trap="parked sweep value is cash to one component and exposure to another",
    )


# ---------------------------------------------------------------------------
# Trap 5 — a bar window containing a halted, zero-volume session.
# ---------------------------------------------------------------------------

def halted_session_bars(
    *, count: int = 30, halted_index_from_end: int = 6,
) -> list[OHLCV]:
    """Daily bars with one HALTED session: a real print, zero volume.

    A halted day is not missing data — the exchange published a bar and the
    volume was genuinely zero. Three live definitions of "average dollar
    volume" disagree about it:

        20 bars, zero-volume bars KEPT     (pipeline admission gate)
        20 bars, zero-volume bars DROPPED  (pipeline volume stats)
        21 bars, zero-volume bars KEPT     (data/context)

    so the same symbol gets three different liquidity numbers, and a symbol
    sitting near the admission floor is admitted or refused depending on which
    one a caller happens to reach for.

    `halted_index_from_end` is inside the trailing 20 by default, so the halt
    lands in every window and the divergence is forced.
    """
    start = date(2026, 7, 1)
    bars: list[OHLCV] = []
    for i in range(count):
        # A gentle uptrend so the 21st bar (the one only `context` sees) has a
        # materially different price and volume from the 20 the others see.
        close = 100.0 + i * 0.5
        volume = 1_000_000 + i * 10_000
        if i == count - 1 - halted_index_from_end:
            volume = 0  # halted session
        bars.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=close - 0.4,
                high=close + 1.1,
                low=close - 1.3,
                close=close,
                volume=volume,
            )
        )
    return bars


def trending_bars(count: int = 60) -> list[OHLCV]:
    """Ordinary bars with real range and no halts — the ATR comparison input.

    Deliberately NOT smooth: a simple moving average of true ranges and Wilder's
    smoothing converge on flat data and diverge on data with volatility
    clustering, which is the only condition under which the ATR defect is
    measurable. The burst below supplies that clustering.
    """
    start = date(2026, 5, 1)
    bars: list[OHLCV] = []
    close = 100.0
    for i in range(count):
        # A volatility burst in the middle third — Wilder still carries weight
        # from it after a simple MA has dropped it out of the window entirely.
        burst = 4.0 if count // 3 <= i < count // 3 + 8 else 1.0
        close = close + (0.6 if i % 3 else -0.4) * burst
        bars.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=close - 0.3 * burst,
                high=close + 1.2 * burst,
                low=close - 1.4 * burst,
                close=close,
                volume=1_000_000 + i * 5_000,
            )
        )
    return bars


#: Every book fixture, for tests that want to sweep all of them.
ALL_BOOKS = (
    ordinary_long_book,
    leveraged_inverse_book,
    net_short_book,
    winning_short_book,
    sweep_parked_book,
)

#: The hostile ones only — `ordinary_long_book` is the control and is EXPECTED
#: to make naive and correct implementations agree.
ADVERSARIAL_BOOKS = (
    leveraged_inverse_book,
    net_short_book,
    winning_short_book,
    sweep_parked_book,
)
