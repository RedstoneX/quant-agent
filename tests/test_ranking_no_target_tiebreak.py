"""Ranking never breaks a score tie on reward:risk (owner ruling 9 Oct 2026).

The ratio's reward side is the entry take-profit target, which is not a sell
rule and is unscored until 12 Oct; its risk side is a model-written stop. An
unverifiable number must never rank a trade, so two candidates tied on score
are ordered by their measured level touches, then symbol — never by the
ratio, whether it comes from the verdict's own evidence or from the desk's
real-ratio preview.
"""

from __future__ import annotations

from src.models import AnalystVerdict, VerdictEvidence
from src.verdicts import rank_verdicts


def _verdict(symbol: str, *, risk_reward: float, touches: float) -> AnalystVerdict:
    return AnalystVerdict(
        seat="technical",
        symbol=symbol,
        direction="bullish",
        magnitude=0.5,
        conviction="medium",
        evidence=[
            VerdictEvidence(label="stop_loss", value=95.0),
            VerdictEvidence(label="risk_reward", value=risk_reward),
            VerdictEvidence(label="stop_side_level_touches", value=touches),
        ],
        invalidation="closes below 95",
    )


def _tied_pair() -> list[AnalystVerdict]:
    # "A" sorts first alphabetically AND has the higher reward:risk, but
    # FEWER level touches — only level touches may decide.
    return [
        _verdict("AAA", risk_reward=4.0, touches=2.0),
        _verdict("ZZZ", risk_reward=1.2, touches=6.0),
    ]


def test_score_tie_falls_to_level_touches_not_verdict_reward_risk():
    ranked = rank_verdicts(_tied_pair())
    assert ranked[0].score == ranked[1].score
    assert [c.symbol for c in ranked] == ["ZZZ", "AAA"]


def test_score_tie_falls_to_level_touches_not_real_reward_risk_preview():
    ranked = rank_verdicts(_tied_pair(), real_reward_risk={"AAA": 5.0, "ZZZ": 0.5})
    assert ranked[0].score == ranked[1].score
    assert [c.symbol for c in ranked] == ["ZZZ", "AAA"]


def test_reward_risk_is_still_recorded_on_the_candidate():
    ranked = rank_verdicts(_tied_pair())
    by_symbol = {c.symbol: c for c in ranked}
    assert by_symbol["AAA"].components["risk_reward_tiebreak"] == 4.0
    assert by_symbol["ZZZ"].components["risk_reward_tiebreak"] == 1.2
