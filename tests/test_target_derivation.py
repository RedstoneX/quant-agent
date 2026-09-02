"""The take-profit is COMPUTED, not guessed (2026-09-01).

Reward:risk is `(target - entry) / (entry - stop)`. The stop has been derived
from measured volatility since 2026-08-27; the target was still the language
model's `reference_target`, so the gate divided a measurement by an opinion.
On the morning run of 2026-09-01 (`run-64290730`) that arithmetic put 30 of 38
actionable signals (79%) under the 1.5 floor before any judgement was applied,
including the two highest-conviction calls of the day, and the desk placed no
trades.

The floor is not the defect and is not touched here. These tests pin its
numerator: derived from the same bars the stop comes from, working in both
directions, and refusing BY NAME rather than falling back to a fabricated
default — a manufactured target with better provenance is still manufactured.
"""

from datetime import date, timedelta

from src.data.levels import (
    MAX_HORIZON_SESSIONS,
    derive_structural_target,
    find_structural_levels,
)
from src.models import (
    OHLCV,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
)
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bars(prices: list[float], *, spread: float = 0.4) -> list[OHLCV]:
    start = date(2024, 1, 1)
    return [
        OHLCV(
            date=start + timedelta(days=i), open=close, high=close + spread,
            low=close - spread, close=close, volume=1_000_000,
        )
        for i, close in enumerate(prices)
    ]


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    """A path that repeatedly turns at `low` and `high` — the thing that makes
    a level a level rather than a coincidence."""
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x", support_resistance="x",
    )


def _analysis(
    *, symbol: str, rating: str, entry: float, stop: float, model_target: float,
    levels: list[float], atr: float, horizon: int = 20, setup: str = "range",
    computed: list[float] | None = None,
) -> TechAnalysisResult:
    """`levels` are the analyst's own (the validator requires at least one for
    an actionable rating); `computed` is what Python found over the full
    history. They are separate parameters because the interesting failure is
    when they DISAGREE — the model naming levels the chart does not support.
    """
    return TechAnalysisResult(
        symbol=symbol, rating=rating, entry_price=entry, stop_loss=stop,
        reference_target=model_target, reasoning="test",
        support_levels=[lv for lv in levels if lv < entry],
        resistance_levels=[lv for lv in levels if lv > entry],
        computed_levels=levels if computed is None else computed, atr_14=atr,
        setup_type=setup, expected_horizon_sessions=horizon,
        reasoning_chain=_tech_rc(),
    )


# ---------------------------------------------------------------------------
# A computed target is produced for a realistic long AND a realistic short
# ---------------------------------------------------------------------------

class TestBothDirections:
    def test_a_long_targets_the_nearest_resistance_above_entry(self):
        """Bars in, level out. Nothing here consults a model."""
        bars = _bars(_oscillation(90.0, 110.0, cycles=20) + [96.0, 97.0, 98.0])
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]
        assert levels, "fixture must produce real structure"

        result = derive_structural_target(
            entry_price=98.0, direction="long", levels=levels,
            atr=2.0, horizon_sessions=25, setup_type="range",
            model_target=104.0,
        )
        assert result.price is not None
        assert result.basis == "structural_level"
        assert result.price > 98.0
        # The nearest computed level above entry, not the furthest and not
        # the model's number.
        assert result.price == min(lv for lv in levels if lv > 98.0 + 2.0)

    def test_a_short_targets_the_nearest_support_below_entry(self):
        """The mirror. A long-only implementation would be a failed one —
        15 of the 38 candidates on 2026-09-01 were bearish."""
        bars = _bars(_oscillation(90.0, 110.0, cycles=20) + [104.0, 103.0, 102.0])
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]
        assert levels

        result = derive_structural_target(
            entry_price=102.0, direction="short", levels=levels,
            atr=2.0, horizon_sessions=25, setup_type="range",
            model_target=96.0,
        )
        assert result.price is not None
        assert result.basis == "structural_level"
        assert result.price < 102.0
        assert result.price == max(lv for lv in levels if lv < 102.0 - 2.0)

    def test_the_two_directions_are_symmetric_about_the_same_chart(self):
        """Same levels, same volatility, same horizon — the only difference
        is which way the trade points."""
        levels = [80.0, 90.0, 110.0, 120.0]
        long_side = derive_structural_target(
            entry_price=100.0, direction="long", levels=levels,
            atr=3.0, horizon_sessions=25, setup_type="range",
        )
        short_side = derive_structural_target(
            entry_price=100.0, direction="short", levels=levels,
            atr=3.0, horizon_sessions=25, setup_type="range",
        )
        assert long_side.price == 110.0
        assert short_side.price == 90.0
        assert long_side.basis == short_side.basis == "structural_level"

    def test_the_constructor_ships_a_computed_take_profit_on_a_short(self):
        """End to end through the order path, not just the pure function."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="TSLA", rating="sell", entry=250.0, stop=262.5,
            model_target=150.0, levels=[220.0, 262.5, 300.0],
            atr=12.5 / 4.0, horizon=45,
        )
        decisions = constructor.construct_orders(
            targets=[TargetPosition(
                symbol="TSLA", direction="short", target_weight_pct=5.0,
                conviction="high", thesis="overvalued",
            )],
            positions=[], analyses=[analysis], total_value=100_000,
            price_map={"TSLA": 250.0},
        )
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.action == "SHORT"
        # The computed level, NOT the model's $150 guess.
        assert decision.take_profit == 220.0
        assert decision.take_profit < decision.entry_price
        assert decision.stop_loss > decision.entry_price


# ---------------------------------------------------------------------------
# Fail closed, by name — never a fabricated fallback
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_insufficient_history_refuses_rather_than_fabricating(self):
        """Four bars cannot produce structure. `find_structural_levels` says
        so honestly (empty lists), and the derivation must decline instead of
        inventing a default — a made-up target is the defect being removed."""
        supports, resistances = find_structural_levels(_bars([100.0] * 4))
        assert (supports, resistances) == ([], [])

        result = derive_structural_target(
            entry_price=100.0, direction="long",
            levels=[lv.price for lv in (*supports, *resistances)],
            atr=2.0, horizon_sessions=20, setup_type="range",
            model_target=120.0,
        )
        assert result.price is None
        assert result.refused
        assert result.refusal == "no_structural_levels"
        assert "insufficient" in result.detail
        # The model's guess survives as evidence and is NOT promoted to the
        # answer just because nothing else was available.
        assert result.model_target == 120.0

    def test_no_volatility_reading_refuses(self):
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[90.0, 115.0],
            atr=None, horizon_sessions=20, setup_type="range",
        )
        assert result.refusal == "no_volatility_reading"

    def test_no_horizon_refuses(self):
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[90.0, 115.0],
            atr=2.0, horizon_sessions=None, setup_type="range",
        )
        assert result.refusal == "no_expected_horizon"

    def test_a_range_setup_with_no_level_in_the_direction_refuses(self):
        """Structure exists but none of it is overhead, and the analyst did
        not call this a breakout. Chart and read disagree — decline."""
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0, 90.0],
            atr=2.0, horizon_sessions=20, setup_type="range",
        )
        assert result.refusal == "no_level_in_direction"
        assert "does not claim a breakout" in result.detail

    def test_each_refusal_names_a_different_thing_being_wrong(self):
        """'No trade' without a reason is what let the original defect
        survive unnoticed. A missing ATR, a missing horizon, an unreadable
        chart and a disagreement about the setup are four different
        problems and must not share one message."""
        codes = {
            derive_structural_target(
                entry_price=100.0, direction="long", levels=[90.0, 115.0],
                atr=None, horizon_sessions=20, setup_type="range").refusal,
            derive_structural_target(
                entry_price=100.0, direction="long", levels=[90.0, 115.0],
                atr=2.0, horizon_sessions=0, setup_type="range").refusal,
            derive_structural_target(
                entry_price=100.0, direction="long", levels=[],
                atr=2.0, horizon_sessions=20, setup_type="range").refusal,
            derive_structural_target(
                entry_price=100.0, direction="long", levels=[80.0],
                atr=2.0, horizon_sessions=20, setup_type="range").refusal,
        }
        assert len(codes) == 4

    def test_the_constructor_declines_the_order_when_derivation_refuses(self):
        """The analyst named levels; Python found none in the history. The
        model's word is not sufficient to open a position."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA", rating="buy", entry=100.0, stop=95.0,
            model_target=130.0, levels=[95.0, 130.0], computed=[],
            atr=1.4, horizon=30,
        )
        decisions = constructor.construct_orders(
            targets=[TargetPosition(
                symbol="NVDA", target_weight_pct=8.0, conviction="high",
                thesis="AI",
            )],
            positions=[], analyses=[analysis], total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert decisions == []


# ---------------------------------------------------------------------------
# The rule's two branches, and where they hand over
# ---------------------------------------------------------------------------

class TestTheRule:
    def test_a_level_inside_the_noise_floor_is_not_a_destination(self):
        """A 'target' half an ATR away is somewhere price already is. Skip it
        and take the next real level out."""
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[100.5, 112.0],
            atr=2.0, horizon_sessions=25, setup_type="range",
        )
        assert result.price == 112.0

    def test_a_breakout_with_no_overhead_level_gets_a_measured_move(self):
        """No ceiling exists, so the only honest statement about where the
        instrument travels is how far it usually travels in the stated
        holding period."""
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[85.0, 92.0],
            atr=2.0, horizon_sessions=25, setup_type="breakout",
        )
        assert result.basis == "measured_move"
        # ATR 2.0 x sqrt(25) x 1.0 = 10.0
        assert result.price == 110.0

    def test_a_measured_move_scales_with_the_square_root_of_the_horizon(self):
        """Daily ranges accumulate as a random walk. Linear ATR x N would
        overstate an N-session excursion by roughly sqrt(N), which is how a
        target becomes a fantasy while still looking arithmetic."""
        four = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0],
            atr=1.0, horizon_sessions=4, setup_type="breakout").price
        sixteen = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0],
            atr=1.0, horizon_sessions=16, setup_type="breakout").price
        assert four == 102.0 and sixteen == 104.0  # 2x horizon-root, not 4x

    def test_a_level_beyond_reach_hands_over_to_the_measured_move(self):
        """Resistance 40% away does not bound a 25-session trade. Nothing
        stands in the way over the hold, so travel governs — and the answer
        is the SMALLER number, which is why this is not target inflation."""
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[140.0],
            atr=2.0, horizon_sessions=25, setup_type="range",
        )
        assert result.basis == "measured_move"
        assert result.price == 110.0
        assert result.level_used == 140.0  # the far level is still recorded

    def test_an_implausible_horizon_cannot_licence_an_arbitrary_target(self):
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0],
            atr=1.0, horizon_sessions=100_000, setup_type="breakout",
        )
        capped = derive_structural_target(
            entry_price=100.0, direction="long", levels=[80.0],
            atr=1.0, horizon_sessions=MAX_HORIZON_SESSIONS, setup_type="breakout",
        )
        assert result.price == capped.price

    def test_a_short_projection_through_zero_is_refused(self):
        result = derive_structural_target(
            entry_price=2.0, direction="short", levels=[1.9],
            atr=1.5, horizon_sessions=25, setup_type="breakout",
        )
        assert result.refusal == "projection_implausible"


# ---------------------------------------------------------------------------
# The model's target is evidence, not arithmetic
# ---------------------------------------------------------------------------

class TestModelTargetIsEvidence:
    def test_the_guess_is_carried_through_and_the_gap_measured(self):
        result = derive_structural_target(
            entry_price=100.0, direction="long", levels=[112.0],
            atr=2.0, horizon_sessions=25, setup_type="range",
            model_target=140.0,
        )
        assert result.price == 112.0
        assert result.model_target == 140.0
        assert result.divergence_pct == -20.0

    def test_the_guess_never_changes_the_answer(self):
        """Same chart, three different model opinions, one computed target."""
        prices = {
            derive_structural_target(
                entry_price=100.0, direction="long", levels=[112.0], atr=2.0,
                horizon_sessions=25, setup_type="range", model_target=guess,
            ).price
            for guess in (105.0, 140.0, None)
        }
        assert prices == {112.0}

    def test_the_order_reasoning_shows_both_numbers(self):
        """The AI Risk Manager reads `reasoning`. It must see that the
        take-profit is computed and where the analyst differed, rather than
        re-deriving the ratio in its head — the failure mode that produced
        two contradictory R/R figures in one response on 2026-08-31."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            symbol="NVDA", rating="buy", entry=100.0, stop=95.0,
            model_target=140.0, levels=[95.0, 112.0],
            atr=5.0 / 4.0, horizon=45,
        )
        decisions = constructor.construct_orders(
            targets=[TargetPosition(
                symbol="NVDA", target_weight_pct=8.0, conviction="high",
                thesis="AI",
            )],
            positions=[], analyses=[analysis], total_value=100_000,
            price_map={"NVDA": 100.0},
        )
        assert len(decisions) == 1
        reasoning = decisions[0].reasoning
        assert "$112.00" in reasoning
        assert "structural level" in reasoning
        assert "$140.00" in reasoning  # the analyst's guess, kept visible


# ---------------------------------------------------------------------------
# The SLB case — the trade this change exists because of
# ---------------------------------------------------------------------------

class TestSLB:
    """SLB, 2026-09-01 morning run: `strong_buy` / `high` conviction, entry
    $60.10, stop $55.50 — a 7.7% stop — and reward:risk 1.28 against the
    model's target, so it was refused before anyone judged it.

    The level data below is SYNTHETIC. The production bars for that session
    are not in this repository, so these fixtures assert what the RULE does
    with a plausible chart, not what SLB's actual chart contained. The
    reported R/R numbers carry that caveat.
    """

    ENTRY = 60.10
    STOP = 55.50
    RISK = ENTRY - STOP                       # $4.60 / share
    MODEL_TARGET = ENTRY + 1.28 * RISK        # ~$65.99, the 1.28 R/R figure
    # The stop is `min_stop_atr_multiple` (3.0) x 1.15 (range setup) = 3.45
    # ATRs out, which back-solves the ATR the run must have been working
    # with. Stated, not assumed silently.
    ATR = RISK / 3.45                         # ~$1.33, 2.2% of price
    FLOOR = 1.5

    def _rr(self, target: float) -> float:
        return round((target - self.ENTRY) / self.RISK, 2)

    def test_the_model_target_is_the_1_28_that_was_rejected(self):
        """Anchor the reproduction: this is the number the run produced."""
        assert self._rr(self.MODEL_TARGET) == 1.28
        assert self._rr(self.MODEL_TARGET) < self.FLOOR

    def test_a_resistance_shelf_above_entry_clears_the_floor(self):
        """The case the change is for. Price topped out repeatedly at ~$67.5
        before the decline into $60; that shelf is where it travels back to,
        and it is further than the model was willing to say."""
        bars = _bars(
            _oscillation(58.0, 67.5, cycles=20)
            + [64.0, 62.0, 61.0, 60.5, 60.1]
        )
        supports, resistances = find_structural_levels(bars)
        levels = [lv.price for lv in (*supports, *resistances)]

        result = derive_structural_target(
            entry_price=self.ENTRY, direction="long", levels=levels,
            atr=self.ATR, horizon_sessions=30, setup_type="range",
            model_target=self.MODEL_TARGET,
        )
        assert result.basis == "structural_level"
        computed_rr = self._rr(result.price)
        # Recorded so the number is visible when this test is read, not just
        # when it fails.
        assert result.price > self.MODEL_TARGET
        assert computed_rr > self._rr(self.MODEL_TARGET)
        assert computed_rr >= self.FLOOR

    def test_a_nearer_shelf_still_fails_the_floor_and_that_is_the_answer(self):
        """The honest other half. If the real chart's nearest resistance is
        $63.50, the computed target is WORSE than the model's guess and SLB
        is correctly refused. The floor does not move to accommodate it."""
        result = derive_structural_target(
            entry_price=self.ENTRY, direction="long",
            levels=[55.0, 63.50, 72.0],
            atr=self.ATR, horizon_sessions=30, setup_type="range",
            model_target=self.MODEL_TARGET,
        )
        assert result.price == 63.50
        assert self._rr(result.price) < self.FLOOR
        # And the disagreement is measured rather than smoothed over.
        assert result.divergence_pct is not None
        assert result.divergence_pct < 0

    def test_the_geometry_refusal_is_distinct_from_a_bad_guess(self):
        """When the stop rule and the computed target cannot make a trade
        together, that is a statement about the trade's shape. It must not
        read as 'the model guessed badly' — the two call for different
        responses from whoever reads the log."""
        constructor = PortfolioConstructor(ConstructorConfig(
            min_reward_risk_after_widening=self.FLOOR,
        ))
        # Structural stop deliberately inside the noise band, so widening
        # fires and the reward:risk gate is reached.
        analysis = _analysis(
            symbol="SLB", rating="buy", entry=self.ENTRY, stop=59.0,
            model_target=self.MODEL_TARGET, levels=[55.0, 63.50],
            atr=self.ATR, horizon=30,
        )
        decisions = constructor.construct_orders(
            targets=[TargetPosition(
                symbol="SLB", target_weight_pct=5.0, conviction="high",
                thesis="oilfield services recovery",
            )],
            positions=[], analyses=[analysis], total_value=100_000,
            price_map={"SLB": self.ENTRY},
        )
        assert decisions == []
