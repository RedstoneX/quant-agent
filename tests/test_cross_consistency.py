"""Layer 2 — do two implementations of the same quantity AGREE?

The 2026-09-01 survey found six defects behind 4,110 passing tests. Not one of
them was a component computing its own answer wrongly; every component was
self-consistent and self-tested. They were SEAM defects — two components each
computing the same named quantity a different way, judged against a shared
threshold, with no test in the repo comparing them.

So these tests never assert that a number is *right*. They assert that two
implementations claiming to produce the same quantity produce the SAME NUMBER
on a book built to separate them (see `tests/book_fixtures.py`).

Legitimate differences, and how they are kept out
-------------------------------------------------
A guard that flags correct code gets switched off within a week, and then there
is nothing. Two consumers may legitimately want different things, and the line
this file draws is:

    DIFFERENT THRESHOLD, same definition  -> fine, and not asserted here.
    DIFFERENT DEFINITION, same name       -> a defect, and asserted here.

The two dollar-volume gates are the worked example. One admits a symbol at one
liquidity floor, the other flags a thin mover at another. Different floors is a
policy choice and this file is silent on it. But both call the result "average
dollar volume", so both must MEASURE it the same way — same window, same
treatment of a halted session. `test_dollar_volume_*` therefore compares the
computed VALUE and never the threshold.

Where an implementation is buried inside a several-hundred-line method and
cannot be called in isolation, that is stated on the test and the arithmetic is
transcribed from the named source line. A transcription can go stale, so it is
`tests/test_one_definition_guard.py` (Layer 3), scanning the real AST, that
holds those sites — not this file. Nothing here is claimed to pin a line of
code it does not actually execute.
"""

from __future__ import annotations

import pytest

from src.risk.rules import _effective_multiplier, _gross_multiplier
from tests.book_fixtures import (
    ADVERSARIAL_BOOKS,
    SWEEP_SYMBOL,
    Book,
    halted_session_bars,
    leveraged_inverse_book,
    net_short_book,
    ordinary_long_book,
    sweep_parked_book,
    trending_bars,
    winning_short_book,
)

# How close two implementations of one quantity must land to count as agreeing.
# Not zero: independent float paths (np.mean vs a Python sum, rounding to 2dp
# inside one implementation and not the other) legitimately differ in the last
# place. 0.5% of the value, or half a percentage point for percentages, is far
# below every divergence the survey measured and far above float noise.
REL_TOL = 0.005
ABS_TOL_PCT = 0.5


def _fmt(book: Book, label: str, values: dict[str, float]) -> str:
    rendered = "\n".join(f"      {k:<52} {v:>12.4f}" for k, v in values.items())
    return (
        f"\n  {label} disagrees across implementations."
        f"\n  book: {book.name} — {book.trap}"
        f"\n{rendered}\n"
        f"  These are the SAME named quantity. One of them is shown to an LLM "
        f"agent\n  and another is enforced by a gate, against the same target."
    )


# ===========================================================================
# Quantity 1 — "how invested is the book".
#
# Both live implementations sit inside large pipeline methods and cannot be
# called in isolation, so the arithmetic below is TRANSCRIBED from the named
# lines. Layer 3 is what pins the real sites; this test's job is to show what
# the divergence actually costs on a real book.
# ===========================================================================

def _invested_pct_pm_view(book: Book) -> float:
    """src/pipeline.py::_build_pm_facts — raw notional, shown to the PM."""
    return (book.total_value - book.cash) / book.total_value * 100


def _invested_pct_rm_view(book: Book) -> float:
    """src/pipeline.py::_filter_hard_risk_decisions — leverage-aware, gates."""
    net = sum(p.market_value * _effective_multiplier(p.symbol) for p in book.positions)
    return abs(net) / book.total_value * 100


@pytest.mark.parametrize("factory", ADVERSARIAL_BOOKS, ids=lambda f: f.__name__)
def test_book_exposure_agrees_between_the_pm_view_and_the_gate(factory):
    """The PM is told one number; the risk gate enforces another. Same target.

    The PM reasons about how much room the book has from `invested_pct`, and
    `max_total_position_pct` refuses trades using its own. If those disagree
    the PM cannot reach a decision the gate will accept — it will either
    withhold buys the book has room for or propose buys that are refused.
    """
    book = factory()
    pm = _invested_pct_pm_view(book)
    rm = _invested_pct_rm_view(book)
    assert abs(pm - rm) <= ABS_TOL_PCT, _fmt(
        book,
        "book exposure %",
        {
            "pipeline.py::_build_pm_facts  (raw, shown to PM)": pm,
            "pipeline.py::_filter_hard_risk_decisions (gate)": rm,
        },
    )


def test_the_control_book_hides_the_exposure_defect():
    """Proof of WHY 4,110 tests missed this — and a guard on the fixtures.

    On a long-only unlevered book the two implementations agree exactly. Any
    test written against such a book passes against both the correct and the
    broken implementation. If this test ever fails, the control fixture has
    stopped being a control and the suite above proves less than it claims.
    """
    book = ordinary_long_book()
    assert abs(_invested_pct_pm_view(book) - _invested_pct_rm_view(book)) < 1e-9


# ===========================================================================
# Quantity 2 — position weight (the share of the book one symbol occupies).
# ===========================================================================

def _weight_gross(book: Book, symbol: str) -> float:
    """The definition used by the constructor, the PM and the risk rules."""
    p = book.by_symbol(symbol)
    return p.market_value * _gross_multiplier(symbol) / book.total_value * 100


def _weight_raw(book: Book, symbol: str) -> float:
    """src/pipeline.py::_build_pm_facts ~6334 and ::_build_position_facts ~8785."""
    p = book.by_symbol(symbol)
    return p.market_value / book.total_value * 100


def test_position_weight_agrees_between_the_prompt_and_the_drift_flag():
    """`Weight: 18.0% ... DRIFT` and `drift-flagged: 0`, three lines apart.

    The PM prompt renders the gross weight; the facts block that says how many
    positions are drift-flagged counts against the raw one. Both go into the
    same prompt, so the model is asked to reconcile two numbers that cannot be
    reconciled.
    """
    book = leveraged_inverse_book()
    gross = _weight_gross(book, "SQQQ")
    raw = _weight_raw(book, "SQQQ")
    assert abs(gross - raw) <= ABS_TOL_PCT, _fmt(
        book,
        "position weight % for SQQQ",
        {
            "portfolio_constructor.py::_current_weights (gross)": gross,
            "pipeline.py::_build_pm_facts ~6334        (raw)": raw,
        },
    )


def test_the_real_constructor_weight_is_leverage_aware():
    """Anchors the sanctioned definition against the REAL function.

    `_weight_gross` above is a transcription; this asserts the transcription
    still matches what `PortfolioConstructor._current_weights` actually does,
    so the comparison above cannot quietly drift into testing itself.
    """
    from src.portfolio_constructor import PortfolioConstructor

    book = leveraged_inverse_book()
    weights = PortfolioConstructor._current_weights(book.positions, book.total_value)
    assert weights["SQQQ"] == pytest.approx(_weight_gross(book, "SQQQ"))
    assert weights["SQQQ"] == pytest.approx(120.0), (
        "SQQQ is 40% of the book by market value and -3x levered; its gross "
        f"weight is 120%, not {weights['SQQQ']:.1f}%"
    )


# ===========================================================================
# Quantity 3 — unrealized P&L percent.
# ===========================================================================

def _pnl_pct_naive(p) -> float:
    """src/agents/portfolio_manager.py ~278, src/pipeline.py ~4105/~5342/~6335."""
    cost = p.avg_entry * p.qty if p.avg_entry and p.qty else 0
    return (p.unrealized_pnl / cost * 100) if cost > 0 else 0.0


def _pnl_pct_abs(p) -> float:
    """src/agents/position_reviewer.py ~190 — already correct, and commented."""
    cost = abs(p.avg_entry * p.qty)
    return (p.unrealized_pnl / cost * 100) if cost else 0.0


@pytest.mark.parametrize("symbol", ["TSLA", "AAPL"])
def test_pnl_percent_agrees_between_the_reviewer_and_the_pm(symbol):
    """One component was fixed and the others were not — the seam, exactly.

    `position_reviewer` carries a long comment explaining why the denominator
    must be `abs(avg_entry * qty)`. The fix never propagated, and nothing in
    the repo asserted the two agree, so a winning short still renders as
    `+$10000.00 (+0.0%)` in the PM's prompt.
    """
    book = winning_short_book()
    p = book.by_symbol(symbol)
    naive, corrected = _pnl_pct_naive(p), _pnl_pct_abs(p)
    assert abs(naive - corrected) <= ABS_TOL_PCT, _fmt(
        book,
        f"unrealized P&L % for {symbol}",
        {
            "portfolio_manager.py ~278 (avg_entry*qty, `> 0`)": naive,
            "position_reviewer.py ~190 (abs(avg_entry*qty))": corrected,
        },
    )


def test_two_positions_that_made_the_same_dollars_report_the_same_way():
    """A sanity anchor that does not depend on either implementation.

    The long and the short in this book each made exactly $10,000. Their P&L
    percentages differ only because their cost bases differ. Whatever the
    implementation, neither may report 0.0% while holding a $10,000 gain.
    """
    book = winning_short_book()
    for p in book.positions:
        assert p.unrealized_pnl == pytest.approx(10_000.0)
        assert _pnl_pct_abs(p) > 0.0
        assert _pnl_pct_naive(p) > 0.0, (
            f"{p.symbol} holds a $10,000 gain but its P&L% renders as "
            f"{_pnl_pct_naive(p):+.1f}% — a `cost_basis > 0` guard swallowed a "
            f"short's negative basis (cost = {p.avg_entry * p.qty:,.0f})"
        )


# ===========================================================================
# Quantity 4 — deployable cash. Both sides are REAL code here.
# ===========================================================================

def test_deployable_cash_agrees_between_the_engine_and_the_api(monkeypatch):
    """The engine sizes trades against one figure; the operator sees another.

    Engine: `cash + parked_sweep_value` — money QAMC already owns.
    API:    `max(cash - reserve, 0)`    — a different question entirely.

    Both are published under the name `deployable_cash`.
    """
    from src.api import routes_live

    book = sweep_parked_book()
    parked = book.by_symbol(SWEEP_SYMBOL).market_value

    monkeypatch.setattr(
        routes_live,
        "read_positions",
        lambda *a, **k: {
            "error": None,
            "positions": [
                {
                    "symbol": p.symbol,
                    "market_value": p.market_value,
                    "is_cash_equivalent": p.symbol == SWEEP_SYMBOL,
                }
                for p in book.positions
            ],
        },
    )

    api = routes_live._compute_liquidity(book.cash, book.total_value)
    engine = book.cash + parked

    assert api.deployable_cash is not None, "API produced no deployable_cash"
    assert api.deployable_cash == pytest.approx(engine, rel=REL_TOL), _fmt(
        book,
        "deployable_cash ($)",
        {
            "pipeline.py::_compute_deployable_cash (engine)": engine,
            "api/routes_live.py::_compute_liquidity  (API)": api.deployable_cash,
        },
    )


# ===========================================================================
# Quantity 5 — percent deployed, engine versus dashboard.
# ===========================================================================

def _pct_deployed_frontend(book: Book) -> float:
    """frontend/src/components/HeroBand.tsx ~119, transcribed.

    `(longMv + hedgeMv) / total` — a raw additive sum of market values with no
    leverage multiplier and no sign handling. Drawn against the ENGINE's
    `max_total_position_pct` ceiling, which is measured the other way.
    """
    total = sum(p.market_value for p in book.positions)
    return total / book.total_value * 100 if book.total_value else 0.0


@pytest.mark.parametrize(
    "factory", [leveraged_inverse_book, net_short_book], ids=lambda f: f.__name__
)
def test_percent_deployed_agrees_between_the_engine_and_the_dashboard(factory):
    """The dashboard draws its own number against the engine's ceiling.

    A gauge whose needle and whose redline come from different definitions
    tells the operator the book is safe while the engine is refusing trades,
    or the reverse. This is the one defect a human sees directly.
    """
    book = factory()
    engine = _invested_pct_rm_view(book)
    dash = _pct_deployed_frontend(book)
    assert abs(engine - dash) <= ABS_TOL_PCT, _fmt(
        book,
        "percent deployed %",
        {
            "risk/rules.py ~1383 (signed + levered, the ceiling)": engine,
            "HeroBand.tsx ~119   (raw additive, the needle)": dash,
        },
    )


# ===========================================================================
# Quantity 6 — average dollar volume. Two of the three are REAL calls.
#
# NOTE the deliberate scope limit stated at the top of this file: the two
# consumers apply DIFFERENT liquidity thresholds and that is fine. This test
# compares only the measured value.
# ===========================================================================

def _avg_dollar_volume_admission_gate(bars) -> float:
    """src/pipeline.py::_evaluate_external_admission_gates ~1101, transcribed.

    Trailing 20 bars, zero-volume sessions KEPT. Buried in a method that needs
    a live broker and config, hence transcribed; Layer 3 pins the real line.
    """
    recent = bars[-20:]
    return sum(float(b.close) * float(b.volume) for b in recent) / len(recent)


def test_dollar_volume_agrees_between_the_two_real_implementations():
    """`_missed_ops_quality_metrics` vs `compute_market_context` — real calls.

    They differ in two ways at once: 20 bars versus 21 (`_W_1M`), and a halted
    zero-volume session dropped versus kept. Either alone moves the number
    enough to flip a symbol sitting near a liquidity floor.
    """
    from src.data.context import compute_market_context
    from src.pipeline import _missed_ops_quality_metrics

    bars = halted_session_bars()
    avg_m, _, _ = _missed_ops_quality_metrics(bars, lookback_days=20)
    ctx = compute_market_context(bars)

    assert avg_m is not None and ctx is not None, "fixture produced no metrics"
    pipeline_dollars = avg_m * 1_000_000
    context_dollars = ctx.avg_dollar_volume

    assert context_dollars is not None
    assert pipeline_dollars == pytest.approx(context_dollars, rel=REL_TOL), (
        "\n  average dollar volume disagrees across implementations."
        "\n  book: halted_session_bars — one real zero-volume session"
        f"\n      pipeline.py::_missed_ops_quality_metrics   {pipeline_dollars:>16,.0f}"
        f"\n      data/context.py::compute_market_context    {context_dollars:>16,.0f}"
        f"\n      spread                                     "
        f"{abs(pipeline_dollars - context_dollars) / context_dollars * 100:>15.2f}%"
        "\n  20 bars with zero-volume sessions DROPPED, versus 21 bars with "
        "them KEPT.\n  Same name, same units, different measurement."
    )


def test_dollar_volume_agrees_with_the_admission_gate():
    """The third definition — the one that decides whether a symbol trades."""
    from src.data.context import compute_market_context

    bars = halted_session_bars()
    gate = _avg_dollar_volume_admission_gate(bars)
    ctx = compute_market_context(bars)
    assert ctx is not None and ctx.avg_dollar_volume is not None

    assert gate == pytest.approx(ctx.avg_dollar_volume, rel=REL_TOL), (
        "\n  average dollar volume disagrees across implementations."
        f"\n      pipeline.py ~1101 admission gate (20 bars)  {gate:>16,.0f}"
        f"\n      data/context.py  (21 bars, _W_1M)          "
        f"{ctx.avg_dollar_volume:>16,.0f}"
        "\n  This one decides ADMISSION. A symbol at the liquidity floor is "
        "admitted\n  or refused depending on which implementation the caller "
        "reached for."
    )


# ===========================================================================
# Quantity 7 — average true range. Both sides are REAL calls.
# ===========================================================================

def test_atr_agrees_between_context_and_technical():
    """A simple MA of true ranges versus Wilder's smoothing. Same name.

    `data/context.py::_atr_series` averages true ranges with `np.convolve`;
    `data/technical.py` uses `ta`'s `AverageTrueRange`, which is Wilder. They
    share no name, no file and no signature — grep finds nothing. Only running
    both on the same bars reveals it, which is why this test exists and why
    Layer 3 matches on the arithmetic PATTERN rather than on identifiers.

    ATR sets stop distance, and stop distance sets position size. Two ATRs is
    two different books.
    """
    from src.data.context import _atr_series
    from src.data.technical import compute_indicators

    bars = trending_bars()
    series = _atr_series(bars)
    assert series.size, "fixture produced no ATR series"
    context_atr = float(series[-1])

    wilder_atr = compute_indicators("TEST", bars).atr_14
    assert wilder_atr is not None, "ta produced no ATR"

    divergence = abs(context_atr - wilder_atr) / wilder_atr * 100
    assert context_atr == pytest.approx(wilder_atr, rel=REL_TOL), (
        "\n  average true range disagrees across implementations."
        f"\n      data/context.py::_atr_series   (simple MA)  {context_atr:>10.4f}"
        f"\n      data/technical.py              (Wilder)     {wilder_atr:>10.4f}"
        f"\n      divergence                                  {divergence:>9.2f}%"
        "\n  ATR sets stop distance and stop distance sets position size, so "
        "these\n  are two different books, not two views of one."
    )
