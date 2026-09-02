"""One average true range, and it is Wilder's.

Why this file exists
--------------------
The codebase computed average true range TWICE, two different ways:

* `src/data/technical.py` used `ta.volatility.AverageTrueRange` — Wilder's
  recursive smoothing, the definition every chart package and every trader
  means by "ATR(14)". This is the number the risk path sizes and widens
  stops with.
* `src/data/context.py` convolved the true ranges with a flat 14-wide
  kernel — a SIMPLE moving average of true range. Not ATR. This is the
  number the analyst was shown as `atr_pct`, `atr_percentile` and
  `volatility_state`.

Measured over 101 real symbols x 973 sessions (2022-10 to 2026-08), the two
disagreed by a mean absolute 7.05% and flipped `volatility_state` on 17.2%
of symbol-days. The two are now one function, `technical.atr_series`.

The tests below are deliberately of two kinds. The numeric ones pin the
SHAPE of the smoothing — a simple moving average cannot pass them, because
Wilder's keeps a decaying memory of a volatility shock that a flat kernel
drops off a cliff exactly `period` bars later. The structural ones pin the
fact that there is only ONE implementation, so a second one cannot quietly
appear next to it again.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.data import context as context_mod
from src.data.context import compute_market_context
from src.data.technical import ATR_PERIOD, atr_series, compute_indicators
from src.models import OHLCV

_SRC = Path(__file__).resolve().parents[1] / "src"


# --------------------------------------------------------------------------
# Fixtures: bar builders and independent reference implementations.
# --------------------------------------------------------------------------

def _bars(rows: list[tuple[float, float, float]]) -> list[OHLCV]:
    """(high, low, close) triples -> OHLCV on consecutive calendar days."""
    start = date(2024, 1, 1)
    return [
        OHLCV(
            date=start + timedelta(days=i),
            open=c, high=h, low=lo, close=c, volume=1_000_000,
        )
        for i, (h, lo, c) in enumerate(rows)
    ]


def _true_ranges(rows: list[tuple[float, float, float]]) -> list[float]:
    """Textbook true range, written out longhand so it is checkable by eye."""
    out = []
    for i, (h, lo, c) in enumerate(rows):
        if i == 0:
            out.append(h - lo)
        else:
            prev_close = rows[i - 1][2]
            out.append(max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
    return out


def _wilder_reference(rows, period=ATR_PERIOD) -> list[float]:
    """Wilder's ATR, longhand: seed with the mean of the first `period` true
    ranges, then smooth recursively. Written independently of the production
    code on purpose — it is the oracle, not a second copy of the answer."""
    tr = _true_ranges(rows)
    out = [sum(tr[:period]) / period]
    for i in range(period, len(tr)):
        out.append((out[-1] * (period - 1) + tr[i]) / period)
    return out


def _sma_of_true_range_reference(rows, period=ATR_PERIOD) -> list[float]:
    """The WRONG implementation this fix removed. Kept here as the thing the
    production code must not agree with under a volatility shock."""
    tr = _true_ranges(rows)
    return [
        sum(tr[i - period + 1: i + 1]) / period
        for i in range(period - 1, len(tr))
    ]


#: 60 quiet bars, one violent bar, then quiet again. The shock is what
#: separates the two smoothings: a flat kernel forgets it in one step
#: `period` bars later; Wilder's bleeds it off geometrically.
_SHOCK_INDEX = 60
_QUIET = (101.0, 100.0, 100.5)      # true range 1.0
_SHOCK = (150.0, 100.0, 100.5)      # true range 50.0


def _shock_rows(n: int = 100) -> list[tuple[float, float, float]]:
    rows = [_QUIET] * n
    rows[_SHOCK_INDEX] = _SHOCK
    return rows


def _drifting_rows(n: int = 320) -> list[tuple[float, float, float]]:
    """A rising, mildly noisy tape — the ordinary case, not a shock."""
    rows = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + (0.004 if i % 3 else -0.003)
        span = price * (0.012 + 0.006 * ((i % 7) / 6.0))
        rows.append((price + span / 2, price - span / 2, price))
    return rows


# --------------------------------------------------------------------------
# Numeric: the series IS Wilder's, and is NOT an SMA of true range.
# --------------------------------------------------------------------------

class TestSmoothingIsWilders:

    def test_series_matches_an_independent_wilder_recursion(self):
        rows = _drifting_rows()
        got = atr_series(_bars(rows))
        want = _wilder_reference(rows)
        assert len(got) == len(want)
        for i, (g, w) in enumerate(zip(got, want)):
            assert g == pytest.approx(w, rel=1e-9), f"index {i}"

    def test_a_flat_kernel_cannot_produce_this_series(self):
        """The discriminating test.

        `period` bars after a volatility shock the simple moving average has
        dropped it entirely and reads the quiet level; Wilder's still carries
        (13/14)**period of it. If anyone reintroduces an SMA of true range,
        this is the assertion that breaks.
        """
        rows = _shock_rows()
        got = atr_series(_bars(rows))
        sma = _sma_of_true_range_reference(rows)
        assert len(got) == len(sma)

        # Series index of the shock, then one full `period` past it — the
        # first index whose SMA window no longer contains the shock bar.
        at_shock = _SHOCK_INDEX - (ATR_PERIOD - 1)
        probe = at_shock + ATR_PERIOD
        assert sma[probe] == pytest.approx(1.0, rel=1e-9), (
            "the reference SMA should have forgotten the shock by now"
        )
        assert got[probe] > 2.0 * sma[probe], (
            f"ATR {got[probe]:.4f} is within 2x of the simple moving average "
            f"{sma[probe]:.4f} — this is not Wilder's smoothing"
        )

        # And it decays smoothly rather than stepping off a cliff.
        assert got[probe] > got[probe + 1] > got[probe + 2]

    def test_the_shock_bar_itself_is_not_where_they_differ(self):
        """Guards the test above from passing for the wrong reason: on the
        shock bar the two agree almost exactly, so a test that only looked
        there would be blind."""
        rows = _shock_rows()
        got = atr_series(_bars(rows))
        sma = _sma_of_true_range_reference(rows)
        at_shock = _SHOCK_INDEX - (ATR_PERIOD - 1)
        assert got[at_shock] == pytest.approx(sma[at_shock], rel=1e-9)


class TestSeriesShape:

    def test_warmup_is_trimmed_off_rather_than_zero_filled(self):
        """`ta` returns 0.0 for the first `period - 1` bars, not NaN. Those
        zeros must never reach the percentile: a zero counts as "today's ATR
        is higher than that", which would bias `volatility_state` toward
        expanding on short histories."""
        rows = _drifting_rows(60)
        got = atr_series(_bars(rows))
        assert len(got) == len(rows) - (ATR_PERIOD - 1)
        assert (got > 0).all(), "a zero from the ta warm-up leaked into the series"

    def test_too_few_bars_yields_an_empty_series_not_a_guess(self):
        assert atr_series(_bars(_drifting_rows(ATR_PERIOD - 1))).size == 0
        assert atr_series([]).size == 0

    def test_the_series_is_causal(self):
        """A prefix of the series must equal the series of the prefix —
        otherwise a value shown on the day would not be the value that day's
        history could have produced."""
        rows = _drifting_rows(200)
        full = atr_series(_bars(rows))
        prefix = atr_series(_bars(rows[:150]))
        assert prefix == pytest.approx(full[: len(prefix)], rel=1e-9)


# --------------------------------------------------------------------------
# Wiring: both readers see the same number.
# --------------------------------------------------------------------------

class TestOneNumberEverywhere:

    def test_context_atr_pct_is_the_wilder_atr(self):
        rows = _drifting_rows()
        bars = _bars(rows)
        ctx = compute_market_context(bars)
        assert ctx is not None and ctx.atr_pct is not None
        expected = round(
            _wilder_reference(rows)[-1] / rows[-1][2] * 100.0, 2
        )
        # abs=0.01 absorbs a 2dp rounding boundary, nothing more: an SMA of
        # true range over these bars misses by several times this.
        assert ctx.atr_pct == pytest.approx(expected, abs=0.01)

    def test_indicators_atr_14_is_the_same_series_last_value(self):
        rows = _drifting_rows()
        bars = _bars(rows)
        indicators = compute_indicators("TEST", bars)
        assert indicators.atr_14 == round(float(atr_series(bars)[-1]), 2)

    def test_the_context_block_and_the_risk_path_cannot_disagree(self):
        """The whole point of the fix: `atr_pct` in the analyst's context
        block and `atr_14` on the risk path are the same measurement."""
        rows = _drifting_rows()
        bars = _bars(rows)
        ctx = compute_market_context(bars)
        indicators = compute_indicators("TEST", bars)
        assert ctx is not None and ctx.atr_pct is not None
        assert indicators.atr_14 is not None
        implied = round(indicators.atr_14 / rows[-1][2] * 100.0, 2)
        assert abs(ctx.atr_pct - implied) <= 0.01, (
            f"context says ATR is {ctx.atr_pct}% of price, the risk path's "
            f"atr_14 implies {implied}% — they have drifted apart again"
        )


# --------------------------------------------------------------------------
# Structural: there is only ONE implementation, and it has no averaging
# kernel in it. These are what catch a reintroduction ANYWHERE in src/.
# --------------------------------------------------------------------------

#: Identifiers (not prose) that only appear when code is building true ranges.
_TRUE_RANGE_TOKENS = ("true_range", "trueRange", "AverageTrueRange", "maximum.reduce")

#: Ways to write "average these numbers with a flat window".
_FLAT_KERNEL_TOKENS = ("convolve", ".rolling(", ".ewm(", "np.ones(", "mean(")

#: The one module allowed to know what a true range is.
_OWNER = "data/technical.py"


def _src_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in str(p))


def _code_only(source: str) -> str:
    """`source` with comments and string literals stripped.

    These scans are about what the code DOES. Matching raw text would fire on
    a docstring that merely explains why a simple moving average was wrong —
    a false alarm, and false alarms are how a guard test gets deleted.
    """
    import io
    import tokenize

    kept: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            # Newline between logical lines so two unrelated statements
            # cannot be glued into a token that matches by accident.
            kept.append("\n" if tok.type in (tokenize.NEWLINE, tokenize.NL)
                        else tok.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source
    return "".join(kept)


class TestOnlyOneImplementation:

    def test_no_module_outside_technical_builds_true_ranges(self):
        offenders = []
        for path in _src_files():
            rel = path.relative_to(_SRC).as_posix()
            if rel == _OWNER:
                continue
            code = _code_only(path.read_text())
            hits = [t for t in _TRUE_RANGE_TOKENS if t in code]
            if hits:
                offenders.append(f"{rel}: {hits}")
        assert not offenders, (
            "average true range must be computed in exactly one place "
            f"(src/{_OWNER}); found true-range construction in: {offenders}"
        )

    def test_the_library_atr_is_constructed_exactly_once(self):
        count = sum(
            _code_only(p.read_text()).count("AverageTrueRange(")
            for p in _src_files()
        )
        assert count == 1, (
            f"expected one AverageTrueRange construction in src/, found {count}"
        )

    def test_the_atr_function_contains_no_flat_averaging_kernel(self):
        body = _code_only(inspect.getsource(atr_series))
        assert "AverageTrueRange" in body, (
            "atr_series no longer delegates to Wilder's implementation"
        )
        found = [t for t in _FLAT_KERNEL_TOKENS if t in body]
        assert not found, (
            f"atr_series averages true ranges with {found} — Wilder's "
            "smoothing is recursive, not a flat window"
        )

    def test_context_imports_its_atr_rather_than_defining_one(self):
        """Structural, not textual: `src/data/context.py` must contain no
        function that produces an ATR of its own."""
        tree = ast.parse(Path(context_mod.__file__).read_text())
        local_defs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and ("atr" in n.name.lower() or "true_range" in n.name.lower())
        ]
        assert not local_defs, (
            f"src/data/context.py defines its own ATR helper(s) {local_defs} "
            "instead of importing src.data.technical.atr_series"
        )
        imported = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "src.data.technical"
            and any(a.name == "atr_series" for a in n.names)
            for n in ast.walk(tree)
        )
        assert imported, (
            "src/data/context.py must import atr_series from src.data.technical"
        )
