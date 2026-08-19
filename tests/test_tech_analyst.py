import json
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from src.agents.tech_analyst import TechAnalystAgent
from src.models import OHLCV, TechnicalIndicators


@pytest.fixture
def sample_indicators():
    return TechnicalIndicators(
        symbol="SPY",
        ma_20=505.0,
        ma_50=498.0,
        ma_200=480.0,
        rsi_14=58.0,
        macd=1.5,
        macd_signal=1.2,
        macd_hist=0.3,
        bb_upper=520.0,
        bb_middle=505.0,
        bb_lower=490.0,
        atr_14=8.5,
        volume_change_pct=15.0,
    )


@pytest.fixture
def sample_bars():
    return [
        OHLCV(date=date(2026, 4, 7), open=503.0, high=510.0, low=500.0, close=507.0, volume=1_000_000),
    ]


def _sym_data(symbol: str, bars, indicators):
    # The batch API expects this shape
    return [{"symbol": symbol, "bars": bars, "indicators": indicators}]


def _valid_response_for(symbol: str) -> str:
    """JSON response covering the full v2 schema (reasoning_chain + conviction + reference_target)."""
    return json.dumps([{
        "symbol": symbol,
        "rating": "buy",
        "conviction": "high",
        "entry_price": 507.0,
        "reference_target": 530.0,
        "stop_loss": 494.0,
        "reasoning_chain": {
            "trend": "Above MA20/50/200 stacked bullish.",
            "momentum": "RSI 58 neutral-bullish, MACD hist positive.",
            "volatility": "Mid-band, ATR steady.",
            "volume": "+15% confirms uptrend.",
            "support_resistance": "Support MA50 498, resistance upper band 520.",
        },
        "reasoning": "Clean bullish alignment.",
    }])


@patch("anthropic.Anthropic")
def test_tech_analyst_batch_parses_full_schema(mock_cls, sample_indicators, sample_bars):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=_valid_response_for("SPY"))]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert "SPY" in results
    spy = results["SPY"]
    assert spy.rating == "buy"
    assert spy.conviction == "high"
    assert spy.reference_target == 530.0
    assert spy.stop_loss == 494.0
    assert spy.reasoning_chain is not None
    assert "bullish" in spy.reasoning_chain.trend.lower()


@patch("anthropic.Anthropic")
def test_tech_analyst_bad_response(mock_cls, sample_indicators, sample_bars):
    """2026-08-19 Tech batch-response symbol-loss fix: a non-JSON response
    must NOT silently return an empty dict — the submitted symbol still
    comes back as an explicit key, mapped to None (a visible failure),
    even after the bounded retry (the mock keeps returning the same bad
    text, so retry can't rescue it either)."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="I think it's bullish but I'm not sure")]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))
    assert results == {"SPY": None}
    # Retried exactly once (bounded) — two total LLM calls, not an
    # unbounded retry loop.
    assert mock_client.messages.create.call_count == 2


def test_build_user_message_includes_indicators_and_current_close(sample_indicators, sample_bars):
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(symbols_data=_sym_data("SPY", sample_bars, sample_indicators))
        assert "SPY" in msg
        assert "505.0" in msg      # ma_20
        assert "58.0" in msg       # rsi_14
        assert "ATR=8.5" in msg    # ATR is surfaced for ATR-based stops
        # Renamed from "Current close" (2026-08-19): with intraday context
        # now possible, calling the last completed daily close "current"
        # was the exact ambiguity that let a stale price read as live.
        assert "Last completed close: 507.0" in msg
        # With no prior_ratings passed, no prior line should appear
        assert "Prior rating" not in msg


def test_build_user_message_surfaces_prior_rating_with_age(sample_indicators, sample_bars):
    """When prior_ratings is supplied, the LLM sees a 'Prior rating' context line."""
    from datetime import timedelta
    from src.util.time import et_today

    prior = {
        "SPY": {
            "rating": "buy",
            "conviction": "high",
            "first_seen_date": (et_today() - timedelta(days=4)).isoformat(),
            "last_rating_date": et_today().isoformat(),
            "entry_price": 500.0,
            "stop_loss": 490.0,
            "reference_target": 525.0,
        }
    }
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            prior_ratings=prior,
        )
        assert "Prior rating (context): buy" in msg
        assert "4d ago" in msg
        assert "entry 500.0" in msg  # prior price surfaced


def test_build_user_message_surfaces_valuation_when_provided(sample_indicators, sample_bars):
    valuations = {"SPY": {"trailing_pe": 28.5, "forward_pe": 26.1, "ps_ratio": 4.2}}
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            valuations=valuations,
        )
        assert "Valuation: trailing PE 28.5 | forward PE 26.1 | P/S 4.2" in msg


def test_build_user_message_hides_valuation_when_all_none(sample_indicators, sample_bars):
    """ETFs typically return all-None valuations — should not render the line at all."""
    valuations = {"SPY": {"trailing_pe": None, "forward_pe": None, "ps_ratio": None}}
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            valuations=valuations,
        )
        assert "Valuation:" not in msg


def test_build_user_message_omits_prior_for_new_symbol(sample_indicators, sample_bars):
    """A symbol with no prior entry should not have a Prior rating line."""
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("NEWSYMBOL", sample_bars, sample_indicators),
            prior_ratings={"OTHER_SYMBOL": {"rating": "buy"}},
        )
        assert "Prior rating" not in msg


@patch("anthropic.Anthropic")
def test_tech_analyst_auto_chunks_large_batch(mock_cls, sample_indicators, sample_bars):
    """Batches > 30 symbols are split into chunks of 25 to avoid context overflow."""
    # Build 50 symbols.
    syms = [f"SYM{i:02d}" for i in range(50)]
    data = [
        {"symbol": s,
         "bars": sample_bars,
         "indicators": TechnicalIndicators(**{**sample_indicators.model_dump(), "symbol": s})}
        for s in syms
    ]

    # Each chunked call returns a 25-item valid array (reuse a single template).
    call_counter = {"n": 0}

    def _chunk_response(**kw):
        call_counter["n"] += 1
        chunk_syms = syms[:25] if call_counter["n"] == 1 else syms[25:]
        arr = [json.loads(_valid_response_for(s))[0] for s in chunk_syms]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 1000
        resp.usage.output_tokens = 500
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _chunk_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, merged = agent.analyze_batch(data)

    # All 50 symbols present; 2 LLM calls issued.
    assert len(results) == 50
    assert mock_client.messages.create.call_count == 2
    # Token accounting aggregates across chunks.
    assert merged.tokens_used == 1000 * 2 + 500 * 2
    # Cost accounting also aggregates across chunks — was buggy until
    # 2026-05-13 (the merged AgentResult only summed tokens_used and
    # left input_tokens / output_tokens / cost_usd at their defaults,
    # which made every real morning's tech_analyst row land in DB
    # with cost_usd=NULL → Telegram push showed "$?.??").
    assert merged.input_tokens == 1000 * 2
    assert merged.output_tokens == 500 * 2
    # Note: model used here is 'claude-sonnet-4-6-20250514' which is
    # NOT in cost_table.PRICING — so cost_usd should be None (any
    # unknown-model chunk flags the whole merged value as unknown).
    assert merged.cost_usd is None


@patch("anthropic.Anthropic")
def test_tech_analyst_chunked_merged_cost_sums_when_model_priced(
    mock_cls, sample_indicators, sample_bars, monkeypatch,
):
    """Pin the happy path: when the configured model IS in cost_table.PRICING
    (e.g. claude-opus-4-7), the merged AgentResult.cost_usd is the sum
    of per-chunk costs — not None and not the cost of just the first chunk.

    Uses a test-fixture PRICING with known rates (input=$10/M, output=$50/M)
    instead of reading the live PRICING dict for both sides of the
    comparison. Tautological pattern: if PRICING were zeroed out,
    expected=$0 and actual=$0 — test would pass while production silently
    misreports cost. Fixed fixture rates make a real math regression visible.
    """
    # Pin pricing for this test; monkeypatch auto-reverts at test exit.
    from src import cost_table
    monkeypatch.setitem(
        cost_table.PRICING, "claude-opus-4-7",
        {"input": 10.0, "output": 50.0},
    )

    syms = [f"SYM{i:02d}" for i in range(50)]
    data = [
        {"symbol": s,
         "bars": sample_bars,
         "indicators": TechnicalIndicators(**{**sample_indicators.model_dump(), "symbol": s})}
        for s in syms
    ]

    call_counter = {"n": 0}
    def _chunk_response(**kw):
        call_counter["n"] += 1
        chunk_syms = syms[:25] if call_counter["n"] == 1 else syms[25:]
        arr = [json.loads(_valid_response_for(s))[0] for s in chunk_syms]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 80_000   # realistic tech_analyst chunk
        resp.usage.output_tokens = 12_000
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _chunk_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-opus-4-7")
    _, merged = agent.analyze_batch(data)

    # 2 chunks × (80_000 × $10/M + 12_000 × $50/M) = 2 × $1.40 = $2.80
    expected = 2 * (80_000 * 10.0 + 12_000 * 50.0) / 1_000_000
    assert merged.cost_usd is not None
    assert abs(merged.cost_usd - expected) < 0.01
    assert merged.input_tokens == 80_000 * 2
    assert merged.output_tokens == 12_000 * 2


# ---------------------------------------------------------------------------
# 2026-08-19 Tech batch-response symbol-loss fix.
#
# Production incident: symbols passed the pre-filter and were sent to
# tech_analyst, but silently disappeared during batch parsing — one chunk
# parsed only 1/10 submitted symbols. The old code iterated over whatever
# the LLM returned and computed a `missing` set purely for a WARNING log,
# so an unrepresented symbol was simply absent from the returned dict:
# indistinguishable, downstream, from "never asked about".
#
# Contract now: every submitted symbol is a KEY in the returned dict —
# a TechAnalysisResult (parsed, incl. an explicitly neutral/sell rating)
# or None (visibly failed after a bounded retry). Never absent.
# ---------------------------------------------------------------------------

def _multi_sym_data(symbols, bars, indicators):
    return [
        {"symbol": s, "bars": bars,
         "indicators": TechnicalIndicators(**{**indicators.model_dump(), "symbol": s})}
        for s in symbols
    ]


@patch("anthropic.Anthropic")
def test_short_response_retries_and_recovers_the_missing_symbols(
    mock_cls, sample_indicators, sample_bars,
):
    """The exact incident shape: the first response covers only 1 of 10
    submitted symbols. The bounded retry re-asks for exactly the 9 missing
    ones and recovers them — no symbol silently dropped, and the retry
    prompt carries only the missing symbols (not the whole batch again)."""
    syms = [f"SYM{i:02d}" for i in range(10)]

    calls = {"n": 0, "retry_symbols": None}

    def _respond(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            arr = [json.loads(_valid_response_for("SYM00"))[0]]
        else:
            # Record what the retry actually asked about.
            calls["retry_symbols"] = [
                s for s in syms if f"### {s}" in kw.get("messages", [{}])[0].get("content", "")
            ]
            arr = [json.loads(_valid_response_for(s))[0] for s in syms[1:]]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, merged = agent.analyze_batch(
        _multi_sym_data(syms, sample_bars, sample_indicators)
    )

    assert set(results.keys()) == set(syms), "every submitted symbol must be a key"
    assert all(v is not None for v in results.values()), "retry must recover all 9"
    assert calls["n"] == 2, "exactly one bounded retry"
    assert calls["retry_symbols"] == syms[1:], "retry re-asks only the missing symbols"
    # Retry tokens/cost are merged into the reported result, never dropped.
    assert merged.input_tokens == 1000
    assert merged.output_tokens == 400


@patch("anthropic.Anthropic")
def test_symbols_unresolved_after_retry_are_explicit_none_not_absent(
    mock_cls, sample_indicators, sample_bars,
):
    """When the retry ALSO comes back short, the still-missing symbols are
    returned as explicit None keys — a visible terminal failure — rather
    than vanishing from the dict."""
    syms = ["AAA", "BBB", "CCC"]

    def _respond(**kw):
        # Always answers about AAA only, no matter what was asked.
        arr = [json.loads(_valid_response_for("AAA"))[0]]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(
        _multi_sym_data(syms, sample_bars, sample_indicators)
    )

    assert set(results.keys()) == set(syms)
    assert results["AAA"] is not None
    assert results["BBB"] is None
    assert results["CCC"] is None


@patch("anthropic.Anthropic")
def test_explicitly_neutral_rating_is_a_terminal_outcome_not_a_loss(
    mock_cls, sample_indicators, sample_bars,
):
    """A considered-and-passed symbol (neutral/sell) is a successful
    terminal outcome — it must come back as a real result and must NOT
    trigger the retry path."""
    resp_json = json.dumps([{
        "symbol": "SPY", "rating": "neutral", "conviction": "low",
        "reasoning_chain": {
            "trend": "Flat.", "momentum": "RSI mid.", "volatility": "Quiet.",
            "volume": "Average.", "support_resistance": "Range-bound.",
        },
        "reasoning": "No edge here.",
    }])
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=resp_json)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert results["SPY"] is not None
    assert results["SPY"].rating == "neutral"
    assert mock_client.messages.create.call_count == 1, "no retry for a valid neutral"


@patch("anthropic.Anthropic")
def test_schema_invalid_row_is_retried_then_marked_failed(
    mock_cls, sample_indicators, sample_bars,
):
    """A row that fails TechAnalysisResult validation counts as missing
    (it produced no usable analysis), gets retried, and — if still bad —
    ends as an explicit None rather than a silent omission."""
    bad_row = json.dumps([{
        "symbol": "SPY", "rating": "buy", "conviction": "high",
        # entry_price omitted -> model validator rejects a `buy` without it
        "reasoning_chain": {
            "trend": "x", "momentum": "x", "volatility": "x",
            "volume": "x", "support_resistance": "x",
        },
        "reasoning": "Broken row.",
    }])
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=bad_row)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert results == {"SPY": None}
    assert mock_client.messages.create.call_count == 2


@patch("anthropic.Anthropic")
def test_chunked_batch_never_loses_a_symbol_across_chunks(
    mock_cls, sample_indicators, sample_bars,
):
    """Chunk-level guarantee composes to the batch level: with 50 symbols
    (2 chunks) where the second chunk's response is entirely unusable,
    every one of the 50 is still a key — chunk 1's parsed, chunk 2's
    explicitly None."""
    syms = [f"SYM{i:02d}" for i in range(50)]

    call_counter = {"n": 0}

    def _respond(**kw):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            arr = [json.loads(_valid_response_for(s))[0] for s in syms[:25]]
            text = json.dumps(arr)
        else:
            text = "the model rambled instead of returning JSON"
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(
        _multi_sym_data(syms, sample_bars, sample_indicators)
    )

    assert set(results.keys()) == set(syms), "no symbol may vanish between chunks"
    assert all(results[s] is not None for s in syms[:25])
    assert all(results[s] is None for s in syms[25:])
