"""docs/WORK.md item 54, 2026-09-12 — the stop is always derivable, and the
gate is on its WIDTH, not on whether a level exists under it.

What shipped in the morning (PR #330, "no floor, no trade") was replaced the
same day on sourced research, before it ran a session:

  * No published method refuses a trade for lack of support below.
    Chandelier, Parabolic SAR, the Darvas box bottom and Kullamägi's
    entry-bar low all place a stop with no level at all
    (https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/).
  * The gap branch was backwards, and it is measured: a rising window holds
    as support only ~20% of the time (Bulkowski,
    https://thepatternsite.com/GaugingGaps.html), so the level beneath a
    gap is MORE reachable, not void.
  * The rule was adversely selected: nearness to the 52-week high forecasts
    returns (George & Hwang 2004, Journal of Finance,
    https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf) and the rule
    refused exactly that population.
  * What published practice constrains is the stop's WIDTH; the answer to
    a wide stop is a smaller position (Van Tharp sizing), not refusal.

Pinned here: (1) the level scan's relevance window is still read from the
instrument (kept from #330); (2) an unbacked or missing stop is read from
the instrument — the wider of the ATR noise band and the signal bar's far
edge; (3) a stop wider than the instrument's own reach over the trade's
horizon is refused BY CODE and recorded as data; (4) a listing too young to
measure is refused by its own code, first; (5) under the cap, a wider stop
buys a smaller position at the same dollars of risk; (6) nothing anywhere
refuses for absent structure any more, and the gap branch is gone.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.data import levels as levels_module
from src.data.levels import (
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    derive_structural_target,
    find_structural_levels,
    horizon_reach,
    structural_floor,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW, atr_series
from src.models import OHLCV, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import (
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    STOP_REFUSAL_INSUFFICIENT_HISTORY,
    STOP_REFUSAL_WIDER_THAN_REACH,
    PortfolioConstructor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _bars(prices: list[float], *, spread: float) -> list[OHLCV]:
    start = date(2024, 1, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=c, high=c + spread,
              low=c - spread, close=c, volume=1_000_000)
        for i, c in enumerate(prices)
    ]


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


def _rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x", support_resistance="x",
    )


def _analysis(symbol, *, entry, stop, levels, rating="buy", setup="range",
              target=None, atr=2.0, horizon=20, bars=None,
              bar_low=None, bar_high=None) -> TechAnalysisResult:
    """`computed_levels` is exactly `levels` — the desk's own scan output,
    attached in Python. `bars_available`, `signal_bar_low/high` are the
    other Python-set fields item 54 added; None means "not recorded"."""
    if target is None:
        target = round(entry * (0.9 if rating in ("sell", "strong_sell") else 1.1), 2)
    a = TechAnalysisResult(
        symbol=symbol, rating=rating, entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target] if target else [],
        computed_levels=list(levels),
        computed_level_touches={p: 5 for p in levels},
        setup_type=setup, expected_horizon_sessions=horizon, atr_14=atr,
        reasoning_chain=_rc(),
    )
    a.bars_available = bars
    a.signal_bar_low = bar_low
    a.signal_bar_high = bar_high
    return a


def _target(symbol, direction="long", risk_pct=None) -> TargetPosition:
    if risk_pct is not None:
        return TargetPosition(
            symbol=symbol, direction=direction, risk_allocation_pct=risk_pct,
            conviction="high", thesis="t",
        )
    return TargetPosition(
        symbol=symbol, direction=direction, target_weight_pct=5.0,
        conviction="high", thesis="t",
    )


def _orders(constructor, analysis, direction="long", risk_pct=None, price=None):
    return constructor.construct_orders(
        targets=[_target(analysis.symbol, direction, risk_pct)], positions=[],
        analyses=[analysis], total_value=100_000,
        price_map={analysis.symbol: price or analysis.entry_price},
    )


# ---------------------------------------------------------------------------
# 1. The relevance window is read from the instrument (kept from #330)
# ---------------------------------------------------------------------------

class TestRelevanceWindowIsReadFromTheInstrument:
    def test_a_volatile_name_sees_a_shelf_a_quiet_name_cannot_reach(self):
        path = _oscillation(85.0, 100.0, cycles=4) + [100.0] * 80
        quiet_supports, _ = find_structural_levels(_bars(path, spread=0.3))
        violent_supports, _ = find_structural_levels(_bars(path, spread=4.0))
        assert not any(80 <= lv.price <= 90 for lv in quiet_supports), quiet_supports
        assert any(80 <= lv.price <= 90 for lv in violent_supports), violent_supports

    def test_the_window_is_the_target_derivations_own_reach_at_the_horizon_cap(self):
        atr = 2.35
        window = horizon_reach(atr, MAX_HORIZON_SESSIONS)
        derivation = derive_structural_target(
            entry_price=100.0, direction="long", levels=[90.0, 105.0],
            atr=atr, horizon_sessions=MAX_HORIZON_SESSIONS, setup_type="range",
        )
        assert derivation.horizon_reach == round(window, 4)
        assert window == atr * (MAX_HORIZON_SESSIONS ** 0.5) * MAX_REACH_ATR_MULTIPLE

    def test_the_window_is_bounded_by_the_horizon_cap_not_by_the_caller(self):
        assert horizon_reach(1.0, 100_000) == horizon_reach(1.0, MAX_HORIZON_SESSIONS)

    def test_no_volatility_reading_means_no_levels_not_a_guessed_window(self):
        bars = _bars(_oscillation(95.0, 100.0, cycles=1) + [100.0], spread=0.5)
        assert len(bars) < 14
        assert find_structural_levels(bars) == ([], [])


# ---------------------------------------------------------------------------
# 2. Nothing refuses for absent structure; the gap branch is gone
# ---------------------------------------------------------------------------

class TestNoFloorIsNotARefusal:
    def test_a_long_with_levels_only_overhead_ships_on_an_instrument_read_stop(self):
        """Two shelves overhead, nothing beneath — the case #330 refused.
        The analyst's stop has nothing computed under it, so it is widened
        to the ATR noise band, and the trade ships."""
        constructor = PortfolioConstructor()
        analysis = _analysis("NVDA", entry=100.0, stop=98.0, levels=[110.0, 120.0])
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        band = 100.0 - constructor._stop_atr_multiple(analysis, None) * 2.0
        assert decisions[0].stop_loss == round(band, 2)
        assert constructor.last_refusals == {}

    def test_a_short_with_levels_only_beneath_ships_the_same_way(self):
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "TSLA", entry=100.0, stop=102.0, levels=[90.0, 80.0], rating="sell",
        )
        decisions = _orders(constructor, analysis, direction="short")
        assert [d.action for d in decisions] == ["SHORT"]
        band = 100.0 + constructor._stop_atr_multiple(analysis, None) * 2.0
        assert decisions[0].stop_loss == round(band, 2)
        assert constructor.last_refusals == {}

    def test_a_breakout_at_new_highs_with_nothing_beneath_ships(self):
        """The population George & Hwang found forecasts returns — a name
        at its highs with no floor the scan can name — is not refused."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "OKLO", entry=100.0, stop=97.0, levels=[], setup="breakout",
            bars=LONGEST_INDICATOR_WINDOW,
        )
        # No levels at all is still `_derive_target`'s refusal (PR #326's
        # split owns that); give the chart one level OVERHEAD... no — a
        # breakout has none. Give it a level far below that backs nothing.
        analysis.computed_levels = [40.0]
        analysis.computed_level_touches = {40.0: 5}
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        assert "measured move" in decisions[0].reasoning
        assert constructor.last_refusals == {}

    def test_the_gap_branch_no_longer_exists_anywhere(self):
        """No `unfilled_gap_edge`, no `gap_edge=` on `structural_floor`, no
        gap-edge fields on the analysis. Bulkowski: a rising window holds
        as support ~20% of the time; the level beneath it is reachable."""
        assert not hasattr(levels_module, "unfilled_gap_edge")
        with pytest.raises(TypeError):
            structural_floor([50.0], 80.0, "long", gap_edge=50.0)  # type: ignore[call-arg]
        fields = TechAnalysisResult.model_fields
        assert "unfilled_up_gap_edge" not in fields
        assert "unfilled_down_gap_edge" not in fields
        assert {"signal_bar_low", "signal_bar_high", "bars_available"} <= set(fields)

    def test_the_owners_gap_example_is_now_judged_on_width_not_on_the_gap(self):
        """$45-50 base, gap to $80. The pre-gap shelf IS the nearest floor
        (Bulkowski says it is the reachable one). An analyst leaning the
        stop on it asks for a stop 37% away; what refuses that trade now is
        the WIDTH gate — the stop sits past what the instrument can
        plausibly travel inside the trade — recorded under its own code."""
        pre = _oscillation(45.0, 50.0, cycles=5) + [50.0] * 10
        day_one = _bars(pre + [80.0], spread=0.6)
        supports, _ = find_structural_levels(day_one)
        shelf = structural_floor([lv.price for lv in supports], 80.0, "long")
        assert shelf is not None and 50 <= shelf <= 51
        atr = float(atr_series(day_one)[-1])
        horizon = 20
        reach = horizon_reach(atr, horizon)
        assert 80.0 - shelf > reach, "the numbers must actually make this the width case"

        constructor = PortfolioConstructor()
        analysis = _analysis(
            "GAPD", entry=80.0, stop=shelf, levels=[lv.price for lv in supports],
            atr=atr, horizon=horizon, setup="breakout",
            bars=LONGEST_INDICATOR_WINDOW,  # old enough: this is the width case
        )
        decisions = _orders(constructor, analysis)
        assert decisions == []
        assert constructor.last_refusals["GAPD"]["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH


# ---------------------------------------------------------------------------
# 3. The stop is always derivable
# ---------------------------------------------------------------------------

class TestTheStopIsAlwaysDerivable:
    def test_nothing_typed_means_the_band_not_a_refusal(self):
        constructor = PortfolioConstructor()
        analysis = _analysis("AMD", entry=100.0, stop=95.0, levels=[110.0])
        analysis.__dict__["stop_loss"] = None
        decisions = _orders(constructor, analysis)
        assert [d.action for d in decisions] == ["BUY"]
        band = 100.0 - constructor._stop_atr_multiple(analysis, None) * 2.0
        assert decisions[0].stop_loss == round(band, 2)

    def test_the_signal_bar_wins_when_it_is_wider_than_the_band(self):
        """Kullamägi's placement: under the low of the entry bar. It only
        decides when that low sits past the band — a climactic bar."""
        constructor = PortfolioConstructor()
        probe = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0])
        band_edge = 100.0 - constructor._stop_atr_multiple(probe, None) * 2.0
        wide_bar = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0],
                             bar_low=band_edge - 3.0)
        placed = constructor._widen_stop_past_noise(
            "BAR", wide_bar, 100.0, 98.0, direction="long", target_price=110.0,
        )
        assert placed == pytest.approx(band_edge - 3.0)
        narrow_bar = _analysis("BAR", entry=100.0, stop=98.0, levels=[110.0],
                               bar_low=band_edge + 1.0)
        placed = constructor._widen_stop_past_noise(
            "BAR", narrow_bar, 100.0, 98.0, direction="long", target_price=110.0,
        )
        assert placed == pytest.approx(band_edge)

    def test_the_short_mirror_uses_the_signal_bars_high(self):
        constructor = PortfolioConstructor()
        a = _analysis("SHRT", entry=100.0, stop=102.0, levels=[90.0], rating="sell")
        band_edge = 100.0 + constructor._stop_atr_multiple(a, None) * 2.0
        a.signal_bar_high = band_edge + 2.0
        placed = constructor._widen_stop_past_noise(
            "SHRT", a, 100.0, 102.0, direction="short", target_price=90.0,
        )
        assert placed == pytest.approx(band_edge + 2.0)

    def test_no_atr_and_nothing_typed_is_still_no_stop(self):
        """The one case nothing can be read: no volatility reading. The
        caller rejects None, as before — nothing is invented from a flat
        percentage."""
        constructor = PortfolioConstructor()
        a = _analysis("NOVOL", entry=100.0, stop=95.0, levels=[110.0], atr=None)
        assert constructor._widen_stop_past_noise(
            "NOVOL", a, 100.0, None, direction="long", target_price=110.0,
        ) is None


# ---------------------------------------------------------------------------
# 4. The width gate — refused by code, recorded as data
# ---------------------------------------------------------------------------

class TestTheWidthGate:
    def test_a_stop_past_the_instruments_reach_is_refused_and_recorded(self):
        """ATR 2, horizon 20: reach = 2 x sqrt(20) x 1.5 = 13.42. A
        level-backed stop $20 away is a stop price cannot reach inside
        the trade, so the size computed from it would be fiction."""
        constructor = PortfolioConstructor()
        a = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 110.0])
        assert 20.0 > horizon_reach(2.0, 20)
        decisions = _orders(constructor, a)
        assert decisions == []
        recorded = constructor.last_refusals["WIDE"]
        assert recorded["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH
        assert recorded["direction"] == "long"
        assert "x ATR" in recorded["detail"]
        assert "refused" in constructor.last_drop_reasons["WIDE"]

    def test_the_same_stop_inside_reach_ships_and_sizes_smaller(self):
        """Same dollars of risk, twice the stop distance, half the shares —
        §2.1, the Van Tharp arithmetic. Under the cap, width is answered
        by size, never by refusal."""
        constructor = PortfolioConstructor()
        near = _analysis("NEAR", entry=100.0, stop=94.0, levels=[94.0, 140.0])
        far = _analysis("FAR", entry=100.0, stop=88.0, levels=[88.0, 140.0])
        assert 12.0 < horizon_reach(2.0, 20)
        d_near = _orders(constructor, near, risk_pct=1.0)
        d_far = _orders(constructor, far, risk_pct=1.0)
        assert [d.action for d in d_near] == ["BUY"] and [d.action for d in d_far] == ["BUY"]
        risk_near = d_near[0].allocation_pct / 100 * 100_000 * (100.0 - d_near[0].stop_loss) / 100.0
        risk_far = d_far[0].allocation_pct / 100 * 100_000 * (100.0 - d_far[0].stop_loss) / 100.0
        assert risk_near == pytest.approx(1_000, abs=20)
        assert risk_far == pytest.approx(1_000, abs=20)
        assert d_far[0].allocation_pct == pytest.approx(d_near[0].allocation_pct / 2, abs=0.05)
        assert constructor.last_refusals == {}

    def test_the_gate_applies_to_a_short(self):
        constructor = PortfolioConstructor()
        a = _analysis("SHRT", entry=100.0, stop=120.0, levels=[120.0, 90.0], rating="sell")
        assert _orders(constructor, a, direction="short") == []
        assert constructor.last_refusals["SHRT"]["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH
        assert "above" in constructor.last_refusals["SHRT"]["detail"]

    def test_the_band_itself_is_never_refused(self):
        """The fallback the desk reads for an unbacked stop (2.5 x ATR at
        most 3.0 with the scales) is inside the reach at every permitted
        horizon >= 4 sessions, and float noise must not refuse it."""
        constructor = PortfolioConstructor()
        for horizon in (4, 10, 20, MAX_HORIZON_SESSIONS):
            a = _analysis("BAND", entry=100.0, stop=99.0, levels=[110.0],
                          horizon=horizon, setup="breakout")
            placed = constructor._widen_stop_past_noise(
                "BAND", a, 100.0, 99.0, regime="risk-off", direction="long",
                target_price=110.0,
            )
            assert placed is not None, horizon
        assert constructor.last_refusals == {}

    def test_the_eligibility_preview_refuses_the_same_names(self):
        constructor = PortfolioConstructor()
        a = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 110.0])
        assert constructor.real_reward_risk_preview(a, "long") is None
        assert constructor.last_refusals["WIDE"]["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH
        drained = constructor.drain_refusals()
        assert "WIDE" in drained and constructor.last_refusals == {}

    def test_the_cap_is_the_reach_not_a_daily_range(self):
        """Stated, not hidden: the cap is `horizon_reach` (the desk's own
        estimate, no new constant), NOT Kullamägi's 1 x daily range — at
        which the desk's own 2.5 x ATR fallback would refuse itself."""
        constructor = PortfolioConstructor()
        band_width = constructor._stop_atr_multiple(None, None) * 2.0
        assert band_width > 2.0  # wider than one ATR: his literal cap
        a = _analysis("CAP", entry=100.0, stop=99.0, levels=[110.0], setup="breakout")
        assert constructor._widen_stop_past_noise(
            "CAP", a, 100.0, 99.0, direction="long", target_price=110.0,
        ) is not None


# ---------------------------------------------------------------------------
# 5. Insufficient history — the one narrow refusal on different grounds
# ---------------------------------------------------------------------------

class TestInsufficientHistory:
    def test_a_young_listing_is_refused_by_its_own_code_first(self):
        """Too few sessions usually means no levels either; the honest name
        is `insufficient_history`, not the derivation's data fault."""
        constructor = PortfolioConstructor()
        a = _analysis("DRAM", entry=59.0, stop=55.0, levels=[], bars=112)
        assert _orders(constructor, a) == []
        recorded = constructor.last_refusals["DRAM"]
        assert recorded["refusal"] == STOP_REFUSAL_INSUFFICIENT_HISTORY
        assert "112" in recorded["detail"] and str(LONGEST_INDICATOR_WINDOW) in recorded["detail"]

    def test_the_threshold_is_the_longest_indicator_window(self):
        constructor = PortfolioConstructor()
        ok = _analysis("OK", entry=100.0, stop=95.0, levels=[95.0, 110.0],
                       bars=LONGEST_INDICATOR_WINDOW)
        assert [d.action for d in _orders(constructor, ok)] == ["BUY"]
        short = _analysis("SHORTHIST", entry=100.0, stop=95.0, levels=[95.0, 110.0],
                          bars=LONGEST_INDICATOR_WINDOW - 1)
        assert _orders(constructor, short) == []
        assert constructor.last_refusals["SHORTHIST"]["refusal"] == STOP_REFUSAL_INSUFFICIENT_HISTORY

    def test_an_unknown_count_is_not_judged(self):
        constructor = PortfolioConstructor()
        a = _analysis("OLDROW", entry=100.0, stop=95.0, levels=[95.0, 110.0], bars=None)
        assert [d.action for d in _orders(constructor, a)] == ["BUY"]
        assert constructor.last_refusals == {}

    def test_the_fields_default_to_not_recorded(self):
        """An older persisted row or a hand-built object carries None on
        all three Python-set fields; the constructor judges none of them."""
        a = _analysis("X", entry=100.0, stop=95.0, levels=[95.0])
        assert a.bars_available is None
        assert a.signal_bar_low is None and a.signal_bar_high is None


# ---------------------------------------------------------------------------
# 6. Recorded as data, read by the PM gate and the census
# ---------------------------------------------------------------------------

class TestRecordedAsData:
    def test_decision_stage_files_the_code_beside_the_reason(self, monkeypatch):
        from src import pipeline_stages

        events: list[dict] = []

        def _capture(pipeline, ctx, symbol, stage, outcome, reason="", **details):
            events.append({"symbol": symbol, "stage": stage, "outcome": outcome,
                           "reason": reason, **details})

        monkeypatch.setattr(pipeline_stages, "_record_pipeline_event", _capture)
        constructor = PortfolioConstructor()
        constructor._note_refusal("WIDE", "long", STOP_REFUSAL_WIDER_THAN_REACH, "too wide")
        constructor._note_refusal("DRAM", "long", STOP_REFUSAL_INSUFFICIENT_HISTORY, "young")
        refusals = constructor.drain_refusals()
        dropped = ["WIDE"]
        for sym in dropped:
            r = refusals.get(sym)
            pipeline_stages._record_pipeline_event(
                None, None, sym, "deterministic_gate", "blocked",
                CONSTRUCTOR_REFUSED_EVENT_REASON, refusal=r["refusal"],
                detail=r["detail"], targeted=True,
            )
        for sym, r in refusals.items():
            if sym not in dropped:
                pipeline_stages._record_pipeline_event(
                    None, None, sym, "deterministic_gate", "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON, refusal=r["refusal"],
                    detail=r["detail"], targeted=False,
                )
        by_symbol = {e["symbol"]: e for e in events}
        assert by_symbol["WIDE"]["refusal"] == STOP_REFUSAL_WIDER_THAN_REACH
        assert by_symbol["WIDE"]["targeted"] is True
        assert by_symbol["DRAM"]["refusal"] == STOP_REFUSAL_INSUFFICIENT_HISTORY
        assert by_symbol["DRAM"]["targeted"] is False
        assert constructor.last_refusals == {}

    def test_the_census_attributes_each_code_to_its_own_bucket(self, tmp_path):
        from scripts.blocked_proposals_census import (
            _connect, _load_fills, _load_pairs, _load_recorded_reasons,
            _load_skips, _load_verdicts, classify,
        )
        from src.storage.db import Database

        db = Database(str(tmp_path / "census.db"))
        db.initialize()
        db.insert_specialist_evidence(
            run_id="r1", decision_id="d1", agent_name="portfolio_manager",
            kind="target", scope="symbol", symbol="WIDE",
            evidence_json=json.dumps({"symbol": "WIDE", "risk_allocation_pct": 1.0}),
        )
        db.insert_specialist_evidence(
            run_id="r1", decision_id="d1", agent_name="pipeline",
            kind="pipeline_event", scope="symbol", symbol="WIDE",
            evidence_json=json.dumps({
                "stage": "deterministic_gate", "outcome": "blocked",
                "reason": "constructor_refused",
                "refusal": STOP_REFUSAL_WIDER_THAN_REACH, "detail": "too wide",
            }),
        )
        db.conn.commit()
        con = _connect(tmp_path / "census.db")
        try:
            reason = classify(
                "d1", "WIDE", ordered=_load_pairs(con, "proposed_order"),
                verdicts=_load_verdicts(con), skips=_load_skips(con),
                fills=_load_fills(con), recorded_reasons=_load_recorded_reasons(con),
            )
        finally:
            con.close()
        assert reason == f"constructor_refused:{STOP_REFUSAL_WIDER_THAN_REACH}"

    def test_pm_rule_r6_reads_the_constructors_snapshot_and_nothing_else(self):
        """R6 names a refusal the shared funnel already recorded. It does
        NOT look at the chart: a name with levels only overhead is not
        blocked when the constructor did not refuse it."""
        from src.agents.portfolio_manager import PortfolioManagerAgent

        no_floor = _analysis("NVDA", entry=100.0, stop=96.0, levels=[110.0, 120.0],
                             setup="breakout")
        wide = _analysis("WIDE", entry=100.0, stop=80.0, levels=[80.0, 110.0],
                         setup="breakout")
        constructor = PortfolioConstructor()
        for a in (no_floor, wide):
            constructor.real_reward_risk_preview(a, "long")
        snapshot = dict(constructor.last_refusals)
        registry = {s: {"technical": "bullish", "news": "bullish"} for s in ("NVDA", "WIDE")}
        verdicts = PortfolioManagerAgent.candidate_eligibility(
            analyses=[no_floor, wide], evidence_registry=registry,
            active_state_changes="", allowed_buy_symbols={"NVDA", "WIDE"},
            constructor_refusals_by_symbol=snapshot,
        )
        assert not any(r.startswith("R6") for r in verdicts["NVDA"]), verdicts
        assert any(
            r.startswith("R6") and STOP_REFUSAL_WIDER_THAN_REACH in r
            for r in verdicts["WIDE"]
        ), verdicts
        # Without the snapshot R6 says nothing at all — no chart-shape rule.
        verdicts = PortfolioManagerAgent.candidate_eligibility(
            analyses=[no_floor, wide], evidence_registry=registry,
            active_state_changes="", allowed_buy_symbols={"NVDA", "WIDE"},
        )
        assert not any(r.startswith("R6") for v in verdicts.values() for r in v)


# ---------------------------------------------------------------------------
# docs/WORK.md item 56, 2026-09-13 — the target number and the refusal number
# are now two numbers, and a stop's width has a published reading.
# ---------------------------------------------------------------------------

class TestStopWidthReadingAndSeparation:
    """Item 56: one constant was estimating targets AND refusing trades.

    Nothing here ratifies a threshold — item 56 is still open. These pin
    (a) the two jobs are now two knobs, (b) the reading that converts a
    width into a probability is the published one and not a fitted curve,
    and (c) what the shipped multiple is actually worth.
    """

    def test_target_and_refusal_multiples_are_independent_knobs(self):
        """Moving the stop-width knob must not move the target knob."""
        from src.portfolio_constructor import ConstructorConfig

        cfg = ConstructorConfig()
        assert cfg.max_target_reach_atr_multiple == 1.5
        assert cfg.max_stop_width_reach_atr_multiple == 1.5
        moved = ConstructorConfig(max_stop_width_reach_atr_multiple=3.0)
        assert moved.max_target_reach_atr_multiple == 1.5, (
            "the target estimate must not follow the refusal threshold"
        )

    def test_settings_expose_both_multiples(self):
        """Both knobs exist in the ratified settings model."""
        from pathlib import Path

        from src.config import load_config

        settings = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
        risk = load_config(settings).risk
        assert risk.max_target_reach_atr_multiple == 1.5
        assert risk.max_stop_width_reach_atr_multiple == 1.5

    def test_range_to_sigma_constant_is_the_gaussian_one(self):
        """`ATR_PER_SIGMA` is sqrt(8/pi) — a property of the Gaussian.

        Feller's range result, the one Parkinson (1980) builds the
        extreme-value variance estimator on. Not tunable, not fitted.
        """
        from src.data.levels import ATR_PER_SIGMA

        assert ATR_PER_SIGMA == pytest.approx((8.0 / 3.141592653589793) ** 0.5)
        assert ATR_PER_SIGMA == pytest.approx(1.5958, abs=1e-4)

    def test_touch_probability_matches_the_reflection_principle(self):
        """P(touch) = 2(1 - Phi(z)), z = width_atrs * ATR_PER_SIGMA / sqrt(H)."""
        import math

        from src.data.levels import ATR_PER_SIGMA, touch_probability

        for width, horizon in ((1.0, 1), (2.5, 20), (6.0, 60), (0.5, 5)):
            z = width * ATR_PER_SIGMA / math.sqrt(horizon)
            expected = math.erfc(z / math.sqrt(2.0))
            assert touch_probability(width, horizon) == pytest.approx(expected)

    def test_touch_probability_is_monotone_and_bounded(self):
        from src.data.levels import touch_probability

        assert touch_probability(0.0, 20) == pytest.approx(1.0)
        assert 0.0 < touch_probability(50.0, 20) < 1e-6
        widths = [0.5, 1.0, 2.0, 4.0, 8.0]
        probs = [touch_probability(w, 20) for w in widths]
        assert probs == sorted(probs, reverse=True)
        # Wider horizon, same width: MORE reachable.
        assert touch_probability(2.5, 60) > touch_probability(2.5, 5)

    def test_unreadable_inputs_return_none_rather_than_a_guess(self):
        from src.data.levels import touch_probability

        assert touch_probability(None, 20) is None
        assert touch_probability(2.5, None) is None
        assert touch_probability(2.5, 0) is None
        assert touch_probability(-1.0, 20) is None
        assert touch_probability(float("nan"), 20) is None
        assert touch_probability("wide", 20) is None

    def test_the_shipped_gate_is_a_constant_and_very_low_probability(self):
        """What 1.5 x ATR x sqrt(H) is worth, stated as a reading.

        The cap scales with sqrt(H) exactly as the reading does, so the
        probability it refuses at is the SAME at every horizon: ~1.7%. That
        is the measured answer to "does this gate bind" — it refuses only a
        stop with under a 2% chance of being touched inside the trade.
        """
        import math

        from src.data.levels import touch_probability

        at_the_cap = {
            h: touch_probability(1.5 * math.sqrt(h), h)
            for h in (5, 20, 40, 60)
        }
        for horizon, p in at_the_cap.items():
            assert p == pytest.approx(0.016681, abs=1e-5), (horizon, p)

    def test_the_desks_own_fallback_stop_cannot_trip_the_gate(self):
        """2.5 x ATR is inside 1.5 x ATR x sqrt(H) for every H >= 3.

        Arithmetic, not a measurement: the gate can only ever fire on a
        level-backed or signal-bar stop, never on the band the desk itself
        falls back to. Recorded so that a future change to either number
        has to face this.
        """
        import math

        for horizon in range(3, 61):
            assert 2.5 <= 1.5 * math.sqrt(horizon), horizon
        assert 2.5 > 1.5 * math.sqrt(2)
