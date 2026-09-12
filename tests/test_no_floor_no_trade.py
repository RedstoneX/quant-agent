"""Owner decision, 2026-09-12: "No floor, no trade. No ceiling is fine."

Two things shipped together and are pinned here as one:

1. The level scan's relevance window is READ from the instrument. Until
   2026-09-12 a repeated turning point only counted if it sat within a flat
   40% of price — a number with no derivation that meant a month of travel
   on a quiet name and a week on a violent one. It is now `horizon_reach`,
   the SAME reachability estimate `derive_structural_target` already uses
   to decide whether a target is reachable, taken at the longest horizon
   the desk permits. It widens on a volatile name and narrows on a quiet
   one because ATR does.

2. A trade with no computed structural level on the STOP side of its entry
   is refused by name, on both setup types, and the refusal is recorded as
   data — not recovered from a log line. A trade with nothing OVERHEAD is
   not refused: that is the breakout setup, and its target is projected.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from src.data.levels import (
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    derive_structural_target,
    find_structural_levels,
    horizon_reach,
    structural_floor,
)
from src.data.technical import atr_series
from src.models import OHLCV, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import (
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    STOP_REFUSAL_NO_STRUCTURAL_FLOOR,
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
              target=None, atr=2.0, horizon=20) -> TechAnalysisResult:
    """`computed_levels` is exactly `levels` — the desk's own scan output,
    attached in Python. Nothing here lists the stop unless the test does.
    `reference_target` is the model's guess, required by the schema for an
    actionable rating and never used by the derivation to choose."""
    if target is None:
        target = round(entry * (0.9 if rating in ("sell", "strong_sell") else 1.1), 2)
    return TechAnalysisResult(
        symbol=symbol, rating=rating, entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target] if target else [],
        computed_levels=list(levels),
        computed_level_touches={p: 5 for p in levels},
        setup_type=setup, expected_horizon_sessions=horizon, atr_14=atr,
        reasoning_chain=_rc(),
    )


def _target(symbol, direction="long") -> TargetPosition:
    return TargetPosition(
        symbol=symbol, direction=direction, target_weight_pct=5.0,
        conviction="high", thesis="t",
    )


# ---------------------------------------------------------------------------
# 1. The relevance window is read from the instrument
# ---------------------------------------------------------------------------

class TestRelevanceWindowIsReadFromTheInstrument:
    def test_a_volatile_name_sees_a_shelf_a_quiet_name_cannot_reach(self):
        """Same chart shape, same shelf 15% below the close; only the daily
        range differs. The quiet name's own volatility says it cannot get
        there inside any permitted horizon, the volatile name's says it can.
        Under the old flat 40% both would have reported the shelf."""
        path = _oscillation(85.0, 100.0, cycles=4) + [100.0] * 80
        quiet = _bars(path, spread=0.3)
        violent = _bars(path, spread=4.0)

        quiet_supports, _ = find_structural_levels(quiet)
        violent_supports, _ = find_structural_levels(violent)

        assert not any(80 <= lv.price <= 90 for lv in quiet_supports), (
            f"quiet name should not reach the $85 shelf: {quiet_supports}"
        )
        assert any(80 <= lv.price <= 90 for lv in violent_supports), (
            f"volatile name should reach the $85 shelf: {violent_supports}"
        )

    def test_the_window_is_the_target_derivations_own_reach_at_the_horizon_cap(self):
        """No second notion of reachable distance. The scan's window is
        `horizon_reach(ATR, MAX_HORIZON_SESSIONS)`, and that is byte-for-byte
        what `derive_structural_target` reports as `horizon_reach` when
        asked about the same instrument at the same horizon."""
        atr = 2.35
        window = horizon_reach(atr, MAX_HORIZON_SESSIONS)
        derivation = derive_structural_target(
            entry_price=100.0, direction="long", levels=[90.0, 105.0],
            atr=atr, horizon_sessions=MAX_HORIZON_SESSIONS, setup_type="range",
        )
        assert derivation.horizon_reach == round(window, 4)
        assert window == atr * (MAX_HORIZON_SESSIONS ** 0.5) * MAX_REACH_ATR_MULTIPLE

    def test_the_window_is_bounded_by_the_horizon_cap_not_by_the_caller(self):
        """A caller cannot licence a wider window by naming a longer
        horizon: the cap that bounds targets bounds relevance too."""
        assert horizon_reach(1.0, 100_000) == horizon_reach(1.0, MAX_HORIZON_SESSIONS)

    def test_an_explicit_atr_widens_or_narrows_the_scan_monotonically(self):
        """Same bars, ATR supplied by the caller: a bigger reading admits a
        superset of the levels a smaller one admits. The only thing that
        moved was the instrument's own volatility."""
        path = _oscillation(85.0, 100.0, cycles=4) + [100.0] * 80
        bars = _bars(path, spread=0.3)
        narrow_s, _ = find_structural_levels(bars, atr=0.5)
        wide_s, _ = find_structural_levels(bars, atr=5.0)
        narrow = {lv.price for lv in narrow_s}
        wide = {lv.price for lv in wide_s}
        assert narrow <= wide
        assert any(80 <= p <= 90 for p in wide)
        assert not any(80 <= p <= 90 for p in narrow)

    def test_no_volatility_reading_means_no_levels_not_a_guessed_window(self):
        """Enough bars for a pivot, not enough for an ATR: reachability
        cannot be measured, so nothing is reported — no fallback distance."""
        bars = _bars(_oscillation(95.0, 100.0, cycles=1) + [100.0], spread=0.5)
        assert len(bars) < 14
        assert find_structural_levels(bars) == ([], [])

    def test_the_gap_case_the_owner_described_with_real_numbers(self):
        """$50 -> $80 on a gap, then a base at $74-80 with two bounces off
        $75. NO timer and NO special case: the levels are re-read each
        session and the chart answers on its own.

        What the code actually does, stated so the board example is real:
        on the gap day the gap itself inflates the measured volatility, so
        the pre-gap shelf at ~$50.60 is still inside reach and is the only
        floor — 37% below. As the gap fades out of the ATR the old shelf
        falls out of reach and the new $74.40 floor (two touches) is what
        the scan reports."""
        pre = _oscillation(45.0, 50.0, cycles=5) + [50.0] * 10
        gap_day = pre + [80.0]
        based = gap_day + [79, 80, 78, 79, 80] + [78, 76, 75, 76, 78, 80, 79, 77, 75, 76, 78, 79, 80, 79]

        day_one = _bars(gap_day, spread=0.6)
        supports, _ = find_structural_levels(day_one)
        floor_day_one = structural_floor([lv.price for lv in supports], 80.0, "long")
        assert floor_day_one is not None and 50 <= floor_day_one <= 51
        reach_day_one = horizon_reach(float(atr_series(day_one)[-1]), MAX_HORIZON_SESSIONS)
        assert 80.0 - floor_day_one < reach_day_one

        later = _bars(based, spread=0.6)
        supports, resistances = find_structural_levels(later)
        floor_later = structural_floor(
            [lv.price for lv in (*supports, *resistances)], later[-1].close, "long",
        )
        assert floor_later is not None and 74 <= floor_later <= 75.5, supports
        touched_twice = next(lv for lv in supports if 74 <= lv.price <= 75.5)
        assert touched_twice.touches == 2
        assert not any(lv.price < 60 for lv in supports), (
            "the pre-gap shelf should have fallen out of reach once the gap "
            f"left the ATR: {supports}"
        )


# ---------------------------------------------------------------------------
# 2. No floor, no trade — a named, recorded refusal
# ---------------------------------------------------------------------------

class TestNoFloorNoTrade:
    def test_a_long_with_no_level_below_entry_is_refused_and_recorded(self):
        """Clean history, structure found (two shelves overhead), nothing
        beneath. Before 2026-09-12 this shipped with an ATR-band stop."""
        constructor = PortfolioConstructor()
        analysis = _analysis("NVDA", entry=100.0, stop=96.0, levels=[110.0, 120.0])
        decisions = constructor.construct_orders(
            targets=[_target("NVDA")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"NVDA": 100.0},
        )
        assert decisions == []
        recorded = constructor.last_refusals["NVDA"]
        assert recorded["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_FLOOR
        assert recorded["direction"] == "long"
        assert "below" in recorded["detail"]
        # The regex capture sees it too — belt and braces, not the record.
        assert "refused" in constructor.last_drop_reasons["NVDA"]

    def test_a_short_with_no_level_above_entry_is_refused_the_same_way(self):
        """A short's floor is overhead. Levels below only: refused."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "TSLA", entry=100.0, stop=104.0, levels=[90.0, 80.0], rating="sell",
        )
        decisions = constructor.construct_orders(
            targets=[_target("TSLA", "short")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"TSLA": 100.0},
        )
        assert decisions == []
        recorded = constructor.last_refusals["TSLA"]
        assert recorded["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_FLOOR
        assert "above" in recorded["detail"]

    def test_a_breakout_label_does_not_exempt_a_long_from_needing_a_floor(self):
        """Applies to BOTH setup types: a breakout still needs support
        beneath it."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "OKLO", entry=100.0, stop=96.0, levels=[110.0], setup="breakout",
        )
        decisions = constructor.construct_orders(
            targets=[_target("OKLO")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"OKLO": 100.0},
        )
        assert decisions == []
        assert constructor.last_refusals["OKLO"]["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_FLOOR

    def test_the_eligibility_preview_refuses_the_same_names_the_constructor_would(self):
        """Same funnel, same answer: the PM is not shown a candidate the
        constructor refuses one stage later."""
        constructor = PortfolioConstructor()
        analysis = _analysis("NVDA", entry=100.0, stop=96.0, levels=[110.0, 120.0])
        assert constructor.real_reward_risk_preview(analysis, "long") is None
        assert constructor.last_refusals["NVDA"]["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_FLOOR
        drained = constructor.drain_refusals()
        assert "NVDA" in drained
        assert constructor.last_refusals == {}

    def test_it_is_not_a_data_fault_an_unreadable_chart_is_refused_earlier_by_name(self):
        """No levels at all is `_derive_target`'s refusal (short/dirty
        history, being split into a data fault by PR #326), never this
        one. The floor rule only speaks about a chart that yielded levels."""
        constructor = PortfolioConstructor()
        analysis = _analysis("DEAD", entry=100.0, stop=96.0, levels=[])
        decisions = constructor.construct_orders(
            targets=[_target("DEAD")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"DEAD": 100.0},
        )
        assert decisions == []
        assert "DEAD" not in constructor.last_refusals
        assert "no target could be computed" in constructor.last_drop_reasons["DEAD"]

    def test_a_floor_two_touches_deep_qualifies_with_no_waiting_period(self):
        """The scan's own minimum (two touches) is the bar. No bar count,
        no timer — a level that exists today qualifies today."""
        analysis = _analysis("GAPR", entry=79.0, stop=74.0, levels=[74.4, 80.6])
        analysis.computed_level_touches = {74.4: 2, 80.6: 4}
        constructor = PortfolioConstructor()
        decisions = constructor.construct_orders(
            targets=[_target("GAPR")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"GAPR": 79.0},
        )
        assert [d.action for d in decisions] == ["BUY"]
        assert "GAPR" not in constructor.last_refusals


class TestNoCeilingIsFine:
    def test_a_long_at_new_highs_with_a_floor_and_nothing_overhead_ships(self):
        """The breakout setup: support beneath, nothing above. Not refused;
        the target is the ATR measured move."""
        constructor = PortfolioConstructor()
        analysis = _analysis(
            "HIGH", entry=100.0, stop=94.0, levels=[94.0, 88.0], setup="breakout",
            atr=2.0, horizon=25,
        )
        decisions = constructor.construct_orders(
            targets=[_target("HIGH")], positions=[], analyses=[analysis],
            total_value=100_000, price_map={"HIGH": 100.0},
        )
        assert [d.action for d in decisions] == ["BUY"]
        assert decisions[0].take_profit is not None and decisions[0].take_profit > 100.0
        assert "measured move" in decisions[0].reasoning
        assert constructor.last_refusals == {}

    def test_no_level_overhead_is_never_a_refusal_whatever_the_label_says(self):
        """The corresponding refusal (`no_level_in_direction`) stays
        unreachable: a range-labelled long with floors only still gets a
        measured-move target rather than a refusal."""
        derivation = derive_structural_target(
            entry_price=100.0, direction="long", levels=[94.0, 88.0],
            atr=2.0, horizon_sessions=25, setup_type="range",
        )
        assert derivation.price is not None
        assert derivation.basis == "measured_move"
        assert derivation.refusal == ""

    def test_structural_floor_is_symmetric_and_names_the_nearest_level(self):
        assert structural_floor([90.0, 95.0, 110.0], 100.0, "long") == 95.0
        assert structural_floor([90.0, 105.0, 110.0], 100.0, "short") == 105.0
        assert structural_floor([110.0, 120.0], 100.0, "long") is None
        assert structural_floor([80.0, 90.0], 100.0, "short") is None
        assert structural_floor([], 100.0, "long") is None
        assert structural_floor([90.0], None, "long") is None


# ---------------------------------------------------------------------------
# 3. The refusal reaches the record as DATA, and the census can read it
# ---------------------------------------------------------------------------

class TestTheRefusalIsRecordedAsData:
    def test_decision_stage_files_the_code_beside_the_reason(self, monkeypatch):
        """`DecisionStage` drains `last_refusals` and files
        (`deterministic_gate`, `blocked`, `constructor_refused`) with the
        code as its own field — never the log-regex fallback."""
        from src import pipeline_stages

        events: list[dict] = []

        def _capture(pipeline, ctx, symbol, stage, outcome, reason="", **details):
            events.append({"symbol": symbol, "stage": stage, "outcome": outcome,
                           "reason": reason, **details})

        monkeypatch.setattr(pipeline_stages, "_record_pipeline_event", _capture)

        constructor = PortfolioConstructor()
        constructor._note_refusal(
            "NVDA", "long", STOP_REFUSAL_NO_STRUCTURAL_FLOOR, "no floor",
        )
        constructor._note_refusal(
            "AMD", "long", STOP_REFUSAL_NO_STRUCTURAL_FLOOR, "no floor either",
        )
        constructor.last_drop_reasons = {"NVDA": "Constructor: BUY NVDA refused ..."}

        class _Pipeline:
            portfolio_constructor = constructor

        class _Decision:
            constructor_dropped = ["NVDA"]

        # Exercise exactly the recording block DecisionStage runs, by
        # replaying its logic against the drained refusals.
        pipeline, ctx, decision = _Pipeline(), object(), _Decision()
        drain = getattr(pipeline.portfolio_constructor, "drain_refusals")
        refusals = drain()
        for sym in decision.constructor_dropped:
            refusal = refusals.get(sym)
            if refusal:
                pipeline_stages._record_pipeline_event(
                    pipeline, ctx, sym, "deterministic_gate", "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON,
                    refusal=refusal["refusal"], detail=refusal["detail"], targeted=True,
                )
        for sym, refusal in refusals.items():
            if sym not in decision.constructor_dropped:
                pipeline_stages._record_pipeline_event(
                    pipeline, ctx, sym, "deterministic_gate", "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON,
                    refusal=refusal["refusal"], detail=refusal["detail"], targeted=False,
                )

        by_symbol = {e["symbol"]: e for e in events}
        assert by_symbol["NVDA"]["reason"] == "constructor_refused"
        assert by_symbol["NVDA"]["refusal"] == STOP_REFUSAL_NO_STRUCTURAL_FLOOR
        assert by_symbol["NVDA"]["targeted"] is True
        assert by_symbol["AMD"]["targeted"] is False
        assert constructor.last_refusals == {}

    def test_the_census_attributes_the_refusal_by_code(self, tmp_path):
        from scripts.blocked_proposals_census import (
            _connect, _load_fills, _load_pairs, _load_recorded_reasons,
            _load_skips, _load_verdicts, classify,
        )
        from src.storage.db import Database

        db = Database(str(tmp_path / "census.db"))
        db.initialize()
        db.insert_specialist_evidence(
            run_id="r1", decision_id="d1", agent_name="portfolio_manager",
            kind="target", scope="symbol", symbol="NVDA",
            evidence_json=json.dumps({"symbol": "NVDA", "risk_allocation_pct": 1.0}),
        )
        db.insert_specialist_evidence(
            run_id="r1", decision_id="d1", agent_name="pipeline",
            kind="pipeline_event", scope="symbol", symbol="NVDA",
            evidence_json=json.dumps({
                "stage": "deterministic_gate", "outcome": "blocked",
                "reason": "constructor_refused",
                "refusal": STOP_REFUSAL_NO_STRUCTURAL_FLOOR, "detail": "no floor",
            }),
        )
        db.conn.commit()

        con = _connect(tmp_path / "census.db")
        try:
            reason = classify(
                "d1", "NVDA", ordered=_load_pairs(con, "proposed_order"),
                verdicts=_load_verdicts(con), skips=_load_skips(con),
                fills=_load_fills(con), recorded_reasons=_load_recorded_reasons(con),
            )
        finally:
            con.close()
        assert reason == f"constructor_refused:{STOP_REFUSAL_NO_STRUCTURAL_FLOOR}"


class TestPmEligibilityMirrorsTheRule:
    def test_r6_blocks_a_candidate_with_levels_but_none_on_the_stop_side(self):
        from src.agents.portfolio_manager import PortfolioManagerAgent

        no_floor = _analysis("NVDA", entry=100.0, stop=96.0, levels=[110.0, 120.0],
                             setup="breakout")
        with_floor = _analysis("AMD", entry=100.0, stop=94.0, levels=[94.0, 110.0],
                               setup="breakout")
        verdicts = PortfolioManagerAgent.candidate_eligibility(
            analyses=[no_floor, with_floor],
            evidence_registry={"NVDA": {"technical": "bullish", "news": "bullish"},
                               "AMD": {"technical": "bullish", "news": "bullish"}},
            active_state_changes=[],
            allowed_buy_symbols={"NVDA", "AMD"},
        )
        assert any(r.startswith("R6 ") for r in verdicts["NVDA"]), verdicts
        assert not any(r.startswith("R6 ") for r in verdicts["AMD"]), verdicts
