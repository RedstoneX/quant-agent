"""Candidate ranking: the agent HOLDS the standalone part instead of inheriting a mixin.

Bodies live in src/agents/portfolio_manager/candidate_ranking.py (`CandidateRanking`).
`hold_candidate_ranking(agent_cls)` builds one part and installs same-named classmethod
delegates on the agent class, so every existing call site (`PortfolioManagerAgent.
candidate_eligibility(...)`, `self.rank_candidates(...)`) keeps resolving. Nothing is
snapshotted: `_macro_parse_failures` is read and assigned on the agent class at call
time with the body's None default, `_macro_sectors` is called through the class, and the
two bodies the part reads through `self.` are handed in live (see held_part.live_body).
The module-level names are re-exported unchanged for the package's patch mirror.
"""

from src.agents.portfolio_manager.candidate_ranking import (  # noqa: F401 — re-exported
    CandidateRanking,
    logger,
)
from src.agents.portfolio_manager.held_part import hold, live_body

_HOLDER = "_candidate_ranking"
_BODIES = "src/agents/portfolio_manager/candidate_ranking.py"

#: Every name the agent exposes for candidate ranking, delegated to the held part.
DELEGATED = (
    'candidate_eligibility', '_collect_seat_verdicts', 'rank_candidates',
    '_apply_conviction_bar', '_render_candidate_ranking',
)

#: Bodies the part owns that it also reads through `self.` — handed in live so a swap
#: on the agent class after construction is what the body sees.
LIVE_BODIES = ('_collect_seat_verdicts', 'candidate_eligibility',)


def build_candidate_ranking(agent_cls) -> CandidateRanking:
    """The part, wired to `agent_cls` live; no agent object is constructed."""
    return CandidateRanking(
        get_macro_parse_failures=lambda: getattr(agent_cls, "_macro_parse_failures", None),
        set_macro_parse_failures=lambda value: setattr(agent_cls, "_macro_parse_failures", value),
        macro_sectors=lambda *args, **kwargs: agent_cls._macro_sectors(*args, **kwargs),
        **{attr.lstrip("_"): live_body(agent_cls, _HOLDER, attr) for attr in LIVE_BODIES},
    )


def hold_candidate_ranking(agent_cls, part: CandidateRanking | None = None):
    """Make `agent_cls` hold one `CandidateRanking` and delegate the ranking names to it."""
    part = part if part is not None else build_candidate_ranking(agent_cls)
    return hold(agent_cls, holder_attr=_HOLDER, part=part, delegated=DELEGATED, bodies_module=_BODIES)
