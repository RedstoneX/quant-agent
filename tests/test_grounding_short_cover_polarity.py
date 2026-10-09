"""Grounding polarity on short COVERS reads the order side, not the intent label.

2026-10-09: five FLNC cover decisions labelled bearish technical evidence
"conflicts" — correct, since bearish evidence argues for keeping a short —
and grounding refused them, because polarity was read as
`wants_bullish = intent == "buy"`. A "sell" intent on a held short is a
cover, a buy-side order: bullish evidence supports it, bearish conflicts.
"""

from datetime import date

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import PortfolioDecision, Position, SmartMoneyFinding, SmartMoneyObservation

from tests.test_pm_grounding import _analysis


def _held(symbol: str, qty: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=20.0,
        current_price=20.0,
        market_value=qty * 20.0,
        unrealized_pnl=0.0,
        sector="Industrials",
    )


def _target(symbol: str, risk: float, direction: str, provenance: list[dict]) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "risk_allocation_pct": risk,
        "conviction": "medium",
        "thesis": "polarity",
        "provenance": provenance,
    }


def _tech(stance: str, relationship: str) -> dict:
    return {"source": "technical", "observed_stance": stance, "relationship": relationship, "evidence": "scan"}


def _errors(target: dict, *, rating: str, positions: list[Position], smart_money=None) -> list[str]:
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            },
            "targets": [target],
            "portfolio_view": "polarity",
        }
    )
    return PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=[_analysis(target["symbol"], rating)],
        positions=positions,
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=None,
        total_value=100_000,
        smart_money_findings=smart_money,
        existing_risk_pct={p.symbol: 1.5 for p in positions},
    )


SHORT = [_held("FLNC", -100.0)]
LONG = [_held("AAPL", 100.0)]


# --- full cover of a held short -------------------------------------------


def test_full_cover_with_bearish_evidence_labelled_conflicts_is_accepted():
    target = _target("FLNC", 0.0, "short", [_tech("sell", "conflicts")])
    assert _errors(target, rating="sell", positions=SHORT) == []


def test_full_cover_with_bullish_evidence_labelled_supports_is_accepted():
    target = _target("FLNC", 0.0, "short", [_tech("buy", "supports")])
    assert _errors(target, rating="buy", positions=SHORT) == []


def test_full_cover_refuses_bearish_evidence_labelled_supports():
    target = _target("FLNC", 0.0, "short", [_tech("sell", "supports")])
    errors = _errors(target, rating="sell", positions=SHORT)
    assert any("does not support the proposed sell" in e for e in errors)


def test_full_cover_refuses_bullish_evidence_labelled_conflicts():
    target = _target("FLNC", 0.0, "short", [_tech("buy", "conflicts")])
    errors = _errors(target, rating="buy", positions=SHORT)
    assert any("cannot be labelled conflicts" in e for e in errors)


# --- partial cover (risk below current on the same short side) ------------


def test_partial_cover_with_bearish_evidence_labelled_conflicts_is_accepted():
    target = _target("FLNC", 0.5, "short", [_tech("sell", "conflicts")])
    assert _errors(target, rating="sell", positions=SHORT) == []


def test_partial_cover_with_bullish_evidence_labelled_supports_is_accepted():
    target = _target("FLNC", 0.5, "short", [_tech("buy", "supports")])
    assert _errors(target, rating="buy", positions=SHORT) == []


def test_partial_cover_refuses_bullish_evidence_labelled_conflicts():
    target = _target("FLNC", 0.5, "short", [_tech("buy", "conflicts")])
    errors = _errors(target, rating="buy", positions=SHORT)
    assert any("cannot be labelled conflicts" in e for e in errors)


# --- unchanged: long close, short entry -----------------------------------


def test_long_close_polarity_unchanged():
    ok = _target("AAPL", 0.0, "long", [_tech("sell", "supports")])
    assert _errors(ok, rating="sell", positions=LONG) == []
    bad = _target("AAPL", 0.0, "long", [_tech("sell", "conflicts")])
    errors = _errors(bad, rating="sell", positions=LONG)
    assert any("cannot be labelled conflicts" in e for e in errors)


def test_short_entry_polarity_unchanged():
    ok = _target("TSLA", 2.0, "short", [_tech("strong_sell", "supports")])
    assert _errors(ok, rating="strong_sell", positions=[]) == []
    bad = _target("TSLA", 2.0, "short", [_tech("strong_sell", "conflicts")])
    errors = _errors(bad, rating="strong_sell", positions=[])
    assert any("cannot be labelled conflicts" in e for e in errors)


# --- smart-money corroboration on a cover ---------------------------------


def _insider_buy(symbol: str) -> SmartMoneyFinding:
    return SmartMoneyFinding(
        symbol=symbol,
        stance="bullish",
        economic_role="actionable",
        summary="one disclosed purchase",
        why_now="new disclosure",
        observations=[
            SmartMoneyObservation(
                symbol=symbol,
                stream="insider",
                actor="Example Member",
                direction="buy",
                transaction_date=date(2026, 6, 1),
                disclosure_date=date(2026, 7, 11),
                source_url="https://example.test/filing",
                lag_days=40,
                disclosure_age_days=20,
                freshness="stale",
                economic_role="confirmatory",
            )
        ],
    )


def test_cover_counts_bullish_smart_money_corroborated_by_bullish_technical():
    smart = {"source": "smart_money", "observed_stance": "bullish", "relationship": "supports", "evidence": "filing"}
    target = _target("FLNC", 0.0, "short", [_tech("buy", "supports"), smart])
    errors = _errors(target, rating="buy", positions=SHORT, smart_money=[_insider_buy("FLNC")])
    assert errors == []
