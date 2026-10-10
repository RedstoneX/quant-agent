"""Item 208(a): the ranking composite is strength + conviction only.

Reward:risk is recorded only — since 2026-10-10 (owner ruling 9 Oct) it no
longer even breaks ties — and net evidence is a gate/ceiling, by
recorded decision (docs/INCIDENT_HISTORY.md, 2026-10-01). These tests fail if
either quietly becomes a score input.
"""

from src.models import AnalystVerdict, VerdictEvidence
from src.verdicts import rank_verdicts, score_verdict


def _v(symbol, rr, seat="technical"):
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        invalidation="closes below the stop",
        evidence=[VerdictEvidence(label="stop_loss", value=95.0), VerdictEvidence(label="risk_reward", value=rr)],
    )


def test_reward_risk_does_not_change_the_composite_score():
    assert score_verdict(_v("AAA", 0.1)) == score_verdict(_v("AAA", 9.0))


def test_reward_risk_does_not_order_names_tied_on_score():
    ranked = rank_verdicts([_v("AAA", 1.0), _v("ZZZ", 3.0)])
    assert ranked[0].score == ranked[1].score
    assert [c.symbol for c in ranked] == ["AAA", "ZZZ"]
    assert "risk_reward" not in ranked[0].components
