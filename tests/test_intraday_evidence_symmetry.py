"""`intra_check` stops being blindfolded — audit §6 / spec Phase 4.

Measured over the 10 days to 2026-08-27: `intra_check` cost **$0.222/run**
against `morning`'s $0.221, ~99% of it the Portfolio Manager call, while doing
almost none of the work. It was also the one session in which the PM was
handed a technical-only evidence registry — with that morning's macro regime
and news intelligence already computed, paid for, and sitting on disk.

The original reasoning was sound about the risk and wrong about the remedy.
What must never happen is stale evidence being cited AS FRESH. Deleting the
evidence prevents that, and also prevents the PM from knowing what regime a
14:00 move is happening in. Carrying it forward with its staleness labelled
gets the first without paying the second.
"""

from datetime import timedelta

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import Position, TechAnalysisResult, TechReasoningChain


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x", support_resistance="x",
    )


def _analysis(symbol="NVDA", rating="buy") -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating=rating, entry_price=100.0, stop_loss=90.0,
        reference_target=140.0, reasoning="r",
        support_levels=[90.0], resistance_levels=[140.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(),
    )


MACRO = {
    "regime": "risk-on",
    "equity_outlook": "bullish",
    "sector_guidance": [{"sector": "technology", "stance": "bullish"}],
}


# --------------------------------------------------------------------------
# The registry no longer discards what the session already has
# --------------------------------------------------------------------------

def test_macro_reaches_the_evidence_registry_on_an_intraday_tick():
    """The blindfold: this used to return `{"NVDA": {"technical": ...}}` and
    drop the macro row, so the PM could not cite a regime it had been given."""
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[_analysis()],
        positions=[Position(
            symbol="NVDA", qty=10, avg_entry=90, current_price=100,
            market_value=1000, unrealized_pnl=100, sector="Technology",
        )],
        news_intel=None, earnings_analyses=[], macro_analysis=MACRO,
        symbol_sectors={"NVDA": "Technology"},
    )
    assert registry["NVDA"]["technical"] == "buy"
    assert registry["NVDA"]["macro"] == "bullish"


def test_the_registry_no_longer_branches_on_the_session():
    """`session_type` used to gate the whole registry and now gates nothing —
    it was removed rather than left inert, so no future reader assumes an
    intraday tick is still being filtered somewhere."""
    import inspect

    sig = inspect.signature(PortfolioManagerAgent.build_evidence_registry)
    assert "session_type" not in sig.parameters
    sig = inspect.signature(PortfolioManagerAgent.validate_grounding)
    assert "session_type" not in sig.parameters


# --------------------------------------------------------------------------
# Carry-forward is date-scoped — the grounding property that mattered
# --------------------------------------------------------------------------

class _Pipeline:
    """Minimal stand-in exposing only the two carry-forward helpers."""

    def __init__(self, macro=None, news=None):
        self.macro_store = _Store(macro)
        self.news_store = _NewsStore(news)

    _carry_forward_macro = None  # bound below


class _Store:
    def __init__(self, state):
        self._state = state

    def load_last_state(self):
        return self._state


class _NewsStore:
    def __init__(self, report):
        self._report = report

    def load_daily_report(self, session=None):
        return self._report


def _pipeline(macro=None, news=None):
    from src.pipeline import TradingPipeline

    obj = _Pipeline(macro, news)
    obj._carry_forward_macro = TradingPipeline._carry_forward_macro.__get__(obj)
    obj._carry_forward_news = TradingPipeline._carry_forward_news.__get__(obj)
    return obj


def test_todays_macro_is_carried_forward():
    from src.trading_calendar import et_today

    state = dict(MACRO, date=str(et_today()))
    assert _pipeline(macro=state)._carry_forward_macro()["regime"] == "risk-on"


def test_yesterdays_macro_is_not_carried_forward():
    """`load_last_state` is not date-scoped. Carrying a previous day's regime
    into today's tick is exactly the "stale evidence presented as current"
    failure the blindfold existed to prevent — the fix must not reintroduce
    it in a subtler form."""
    from src.trading_calendar import et_today

    stale = dict(MACRO, date=str(et_today() - timedelta(days=1)))
    assert _pipeline(macro=stale)._carry_forward_macro() is None


def test_absent_macro_leaves_the_tick_exactly_as_blind_as_before():
    assert _pipeline(macro=None)._carry_forward_macro() is None
    assert _pipeline(macro={})._carry_forward_macro() is None


def test_a_macro_store_failure_never_fails_the_tick():
    """The intraday tick's first job is deterministic loss protection. A
    carry-forward problem may cost it context; it must never cost that."""
    class _Broken:
        def load_last_state(self):
            raise RuntimeError("disk gone")

    obj = _pipeline()
    obj.macro_store = _Broken()
    assert obj._carry_forward_macro() is None


def test_a_malformed_news_cache_degrades_to_no_news():
    obj = _pipeline(news={"not": "a valid report"})
    assert obj._carry_forward_news() is None


def test_news_round_trips_from_its_stored_dump():
    """The store holds `NewsIntelligenceReport.model_dump()`, so it must
    re-validate into the same model the morning path produced."""
    from src.models import MacroNarrative, NewsIntelligenceReport

    stored = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-08-27", era_themes=["AI capex"],
            current_regime="risk-on, AI-led rally",
        ),
        state_changes=[], stock_news={},
        pm_briefing="Bullish news", market_sentiment="bullish",
        confidence="medium",
    ).model_dump()
    carried = _pipeline(news=stored)._carry_forward_news()
    assert carried is not None
    assert carried.market_sentiment == "bullish"


def test_undated_macro_is_carried_rather_than_discarded():
    """A stored state with no date field predates the date stamping. Refusing
    it would silently re-blindfold every tick until the next morning write."""
    assert _pipeline(macro=dict(MACRO))._carry_forward_macro()["regime"] == "risk-on"


# --------------------------------------------------------------------------
# The two shapes of sector_guidance
# --------------------------------------------------------------------------

@pytest.mark.parametrize("guidance", [
    # What the live macro agent emits.
    [{"sector": "Technology", "stance": "bullish", "reason": "capex"}],
    # What MacroStore PERSISTS — normalized to {sector: direction}, reasons
    # dropped. Carrying the morning's macro forward is what first put this
    # shape in front of the registry; iterating it as a list yields bare
    # strings and `row.get` raises AttributeError, taking the whole PM call
    # down on every intraday tick.
    {"Technology": "bullish"},
    # Degenerate inputs must fall through to the broad outlook, not raise.
    None, "garbage", 42, [None, "x"],
])
def test_registry_survives_every_sector_guidance_shape(guidance):
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], news_intel=None, earnings_analyses=[],
        positions=[Position(
            symbol="NVDA", qty=10, avg_entry=90, current_price=100,
            market_value=1000, unrealized_pnl=100, sector="Technology",
        )],
        macro_analysis={
            "regime": "risk-on", "equity_outlook": "bullish",
            "sector_guidance": guidance,
        },
        symbol_sectors={"NVDA": "Technology"},
    )
    assert registry["NVDA"]["macro"] == "bullish"


def test_the_stored_macro_shape_reaches_the_registry_intact():
    """End to end: what MacroStore writes must be what the registry reads.
    These two had drifted apart and nothing connected them until now."""
    from src.data.macro_store import _normalize_sector_guidance

    live = [{"sector": "Technology", "stance": "overweight", "reason": "capex"}]
    stored = _normalize_sector_guidance(live)
    assert stored == {"Technology": "bullish"}

    def macro_stance(guidance):
        return PortfolioManagerAgent.build_evidence_registry(
            analyses=[], news_intel=None, earnings_analyses=[],
            positions=[Position(
                symbol="NVDA", qty=10, avg_entry=90, current_price=100,
                market_value=1000, unrealized_pnl=100, sector="Technology",
            )],
            macro_analysis={"regime": "risk-on", "equity_outlook": "neutral",
                            "sector_guidance": guidance},
            symbol_sectors={"NVDA": "Technology"},
        )["NVDA"]["macro"]

    # What matters, and what was broken: both shapes yield a SECTOR-SPECIFIC
    # stance. Before the shape fix the stored form raised AttributeError; a
    # naive repair would instead have swallowed it and silently fallen back to
    # the broad `equity_outlook` ("neutral" here), which looks like working
    # code while quietly discarding the sector view.
    assert macro_stance(live) != "neutral"
    assert macro_stance(stored) != "neutral"

    # And they are now the SAME STRING. The live guidance speaks
    # overweight/underweight, MacroStore persists bullish/bearish, and until
    # the registry normalized both the same macro view reached the PM in one
    # vocabulary in the morning and the other at 14:00 — purely by which
    # session happened to read it. Internally consistent per session, and a
    # trap for whoever next debugs a provenance mismatch ACROSS sessions.
    assert macro_stance(live) == macro_stance(stored) == "bullish"


def test_the_registry_speaks_the_persisted_vocabulary():
    """The direction vocabulary is not the registry's private choice.

    `PositionSnapshot.macro_sector_tailwind` is a hard Literal over
    bullish|neutral|bearish, and the evening thesis-health block reads the
    same persisted strings. Normalizing the registry TOWARD tilts instead
    would have made the PM the only component speaking overweight."""
    from src.models import SECTOR_STANCE_TO_DIRECTION, normalize_sector_stance

    assert set(SECTOR_STANCE_TO_DIRECTION) == {
        "overweight", "neutral", "underweight",
    }
    for tilt, direction in SECTOR_STANCE_TO_DIRECTION.items():
        assert normalize_sector_stance(tilt) == direction
        # Idempotent: the stored shape arrives already normalized.
        assert normalize_sector_stance(direction) == direction
    # Case and whitespace are the model's to get wrong, not ours.
    assert normalize_sector_stance(" Underweight ") == "bearish"
    # An unrecognized stance is dropped, never passed through — the polarity
    # sets in the grounding validator would reject it with an error the model
    # has no way to act on.
    assert normalize_sector_stance("mildly keen") is None
    assert normalize_sector_stance(None) is None


@pytest.mark.parametrize("guidance", [
    [{"sector": "Technology", "stance": "underweight", "reason": "rates"}],
    {"Technology": "bearish"},
])
def test_the_prompt_and_the_registry_state_one_stance(guidance):
    """The PM is told to copy the validated stance exactly, so the rendered
    Macro section and the evidence registry must not disagree.

    The dict shape additionally used to CRASH here: the Sector Guidance
    renderer indexed each entry as a mapping, and iterating the persisted
    form yields bare sector names, so every intraday tick that carried the
    morning's macro forward died on `TypeError: string indices must be
    integers` before the model saw anything."""
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    position = Position(
        symbol="NVDA", qty=10, avg_entry=90, current_price=100,
        market_value=1000, unrealized_pnl=100, sector="Technology",
    )
    macro = {
        "regime": "risk-off", "equity_outlook": "bearish",
        "sector_guidance": guidance, "position_guidance": {},
    }
    message = agent.build_user_message(
        analyses=[], positions=[position], macro_analysis=macro,
        cash_balance=1000.0, total_value=2000.0,
        symbol_sectors={"NVDA": "Technology"},
    )
    guidance_lines = [
        line for line in message.splitlines()
        if line.startswith("- Technology:")
    ]
    assert guidance_lines, "sector guidance never reached the prompt"
    assert "bearish" in guidance_lines[0]
    assert "underweight" not in guidance_lines[0]

    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], news_intel=None, earnings_analyses=[],
        positions=[position], macro_analysis=macro,
        symbol_sectors={"NVDA": "Technology"},
    )
    assert registry["NVDA"]["macro"] == "bearish"


def test_the_live_shape_keeps_its_reason_in_the_prompt():
    """Normalizing the stance must not cost the prompt macro's reasoning —
    that text is the only thing explaining WHY the sector is tilted, and it
    exists solely in the live shape (MacroStore drops it to stay small)."""
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    message = agent.build_user_message(
        analyses=[], positions=[], macro_analysis={
            "regime": "risk-on", "equity_outlook": "bullish",
            "position_guidance": {},
            "sector_guidance": [
                {"sector": "Technology", "stance": "overweight",
                 "reason": "AI capex intact"},
            ],
        },
        cash_balance=1000.0, total_value=1000.0,
    )
    assert "- Technology: bullish — AI capex intact" in message
