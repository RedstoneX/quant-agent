"""An expired news seat must be re-asked with the wire this tick fetched.

2026-09-18, the 13:45 `intra_check`: the expiry peek fetched 88 fresh wire
headlines, used them to prove the remembered morning report superseded,
then threw them away. The seat went `expired` (LOST), the evidence gate
correctly refused a decision, and the desk lost the window having already
paid for current news.

The gate is right and untouched. The defect is upstream: the peek's wire
text must reach the news seat's one paid heal retry. A peek that returned
nothing must still lose the seat.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext


def _stored_report() -> dict:
    from src.models import MacroNarrative, NewsIntelligenceReport

    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-17", era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        state_changes=[],
        stock_news={"AAPL": [{
            "headline": "Apple beats",
            "sentiment": "bullish",
            "conviction": "high",
            "impact_summary": "beat",
        }]},
        pm_briefing="ok", market_sentiment="bullish", confidence="medium",
    ).model_dump()


class _NewsStore:
    def __init__(self, report=None, raw_headlines=None):
        self._report = report
        self._raw = raw_headlines or []

    def load_daily_report(self, session=None):
        return self._report

    def load_raw_headlines(self, session_date=None):
        return list(self._raw)


class _Provider:
    """Wire provider. `titles=None` models a fetch that raised."""

    def __init__(self, titles):
        self.titles = titles

    def fetch_news(self, symbols=None):
        if self.titles is None:
            raise RuntimeError("wire fetch failed")
        items = [SimpleNamespace(title=t, summary="") for t in self.titles]
        return items, None

    def format_for_prompt(self, items, max_items=50):
        if not items:
            return ""
        return "\n".join(f"- {i.title}" for i in items[:max_items])


def _bind(obj):
    for name in (
        "_carry_forward_news",
        "_news_has_newer_material_wire",
        "_watched_research_symbols",
        "_peek_news_headlines",
        "_peeked_news_wire_text",
        "_try_one_paid_research_retry",
    ):
        fn = getattr(TradingPipeline, name, None)
        if fn is not None:
            setattr(obj, name, fn.__get__(obj))
    return obj


def _carry(obj, ctx):
    """Carry news forward the way the intraday caller does.

    Tolerates the pre-fix signature so the failure below is the behaviour
    (no wire reached the heal path), not a TypeError on the call.
    """
    try:
        return obj._carry_forward_news(ctx)
    except TypeError:
        return obj._carry_forward_news()


def _pipeline(titles, *, analyst=None):
    obj = SimpleNamespace(
        news_store=_NewsStore(_stored_report(), raw_headlines=[{"title": "Apple beats"}]),
        news_provider=_Provider(titles),
        config=SimpleNamespace(news=SimpleNamespace(max_prompt_items=50)),
        news_analyst=analyst,
        _require_paid_analysis=lambda name: None,
        _record_heal=lambda ctx, result, alert=False: None,
    )
    return _bind(obj)


class _Analyst:
    """Stands in for news_analyst. Records the text it was handed."""

    def __init__(self):
        self.seen = None

    def analyze(self, news_text, *args, **kwargs):
        self.seen = news_text
        return SimpleNamespace(model_dump=lambda: {"pm_briefing": "re-asked"}), None


def _ctx() -> RunContext:
    return RunContext(run_id="t1", session="intra_check")


def test_expired_news_hands_the_fetched_wire_to_the_heal_path():
    ctx = _ctx()
    obj = _pipeline(["Apple beats", "AAPL guidance cut after close"])

    carried = _carry(obj, ctx)

    # The seat is still expired: nothing weakened the detector or the gate.
    assert carried.status == "expired"
    assert carried.payload is None
    # But the wire text it paid for is now available to re-ask with.
    assert isinstance(ctx.heal_news_text, str)
    assert "AAPL guidance cut after close" in ctx.heal_news_text


def test_expired_news_seat_is_re_run_with_that_wire():
    ctx = _ctx()
    analyst = _Analyst()
    obj = _pipeline(
        ["Apple beats", "AAPL guidance cut after close"], analyst=analyst,
    )
    ctx.data_status = {"news": _carry(obj, ctx).status}
    assert ctx.data_status["news"] == "expired"

    healed = obj._try_one_paid_research_retry(ctx, "news")

    assert healed is True
    assert "AAPL guidance cut after close" in (analyst.seen or "")
    assert ctx.data_status["news"] == "ok"
    assert ctx.news_intel is not None
    # The single-retry cap is untouched and now spent.
    assert ctx.heal_paid_retries.get("news") == 1
    assert obj._try_one_paid_research_retry(ctx, "news") is False


def test_a_failed_peek_still_loses_the_seat_and_keeps_the_retry():
    """A fetch that got nothing must not be dressed up as fresh news."""
    ctx = _ctx()
    analyst = _Analyst()
    obj = _pipeline(None, analyst=analyst)  # fetch raises

    carried = _carry(obj, ctx)

    # A failed fetch is not a supersede, so the remembered report survives.
    assert carried.status != "expired"
    assert ctx.heal_news_text is None
    ctx.data_status = {"news": "carry_forward_failed"}
    assert obj._try_one_paid_research_retry(ctx, "news") is False
    assert analyst.seen is None
    # No inputs → the retry slot was NOT consumed.
    assert ctx.heal_paid_retries.get("news") is None


def test_unchanged_wire_still_reuses_and_feeds_nothing():
    ctx = _ctx()
    obj = _pipeline(["Apple beats"])

    carried = _carry(obj, ctx)

    assert carried.payload is not None
    assert ctx.heal_news_text is None
