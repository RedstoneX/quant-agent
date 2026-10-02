"""Candidate eligibility, seat-verdict collection, ranking, conviction bar and its rendering.

Bodies live in src/agents/portfolio_manager/candidate_ranking.py (`CandidateRanking`);
this mixin keeps same-named thin shims, built per call so a collaborator swapped
after construction is what the body sees. The module-level names are re-exported
unchanged for importers and for the package's patch mirror.
"""

import inspect

from src.agents.portfolio_manager.candidate_ranking import (  # noqa: F401 — re-exported
    CandidateRanking,
    logger,
)
from src.cost_circuit.parts.shim_guard import _is_class_shim

#: Bodies the part owns that it also reads through `self.` — handed back to it only
#: when swapped on the host, never as the mixin's own shim (recursion guard).
_OWN_BODIES = ('_collect_seat_verdicts', 'candidate_eligibility',)


def _is_own_shim(cls, attr: str) -> bool:
    """`_is_class_shim` sees bound methods and partials; a classmethod read off the
    class binds fresh each time, so the raw descriptor is compared as well."""
    own = vars(CandidateRankingMixin).get(attr)
    return own is not None and (
        _is_class_shim(getattr(cls, attr, None), attr, CandidateRankingMixin)
        or inspect.getattr_static(cls, attr, None) is own)


class CandidateRankingMixin:
    """Candidate eligibility, seat-verdict collection, ranking, conviction bar and its rendering."""

    # ------------------------------------------------------------------
    # Phase 13 — candidate eligibility + ranking over the shared verdict shape
    # ------------------------------------------------------------------

    @classmethod
    def _candidate_ranking(cls) -> CandidateRanking:
        """Thin shim: builds the standalone object from this class's collaborators
        (bodies moved to src/agents/portfolio_manager/candidate_ranking.py). Built per
        call so a collaborator swapped after construction is what the body sees."""
        return CandidateRanking(
            # `PortfolioManagerAgent` is wired into this module by the package after the
            # agent class exists (see src/agents/portfolio_manager/__init__.py); read lazily,
            # with the identical None default the body used, so the host state stays live.
            get_macro_parse_failures=lambda: getattr(PortfolioManagerAgent, "_macro_parse_failures", None),  # noqa: F821
            set_macro_parse_failures=lambda value: setattr(PortfolioManagerAgent, "_macro_parse_failures", value),  # noqa: F821
            macro_sectors=lambda *args, **kwargs: PortfolioManagerAgent._macro_sectors(*args, **kwargs),  # noqa: F821
            **{
                attr.lstrip("_"): getattr(cls, attr)
                for attr in _OWN_BODIES
                if not _is_own_shim(cls, attr)
            },
        )

    @classmethod
    def candidate_eligibility(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/candidate_ranking.py."""
        return cls._candidate_ranking().candidate_eligibility(*args, **kwargs)

    @classmethod
    def _collect_seat_verdicts(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/candidate_ranking.py."""
        return cls._candidate_ranking()._collect_seat_verdicts(*args, **kwargs)

    @classmethod
    def rank_candidates(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/candidate_ranking.py."""
        return cls._candidate_ranking().rank_candidates(*args, **kwargs)

    @classmethod
    def _apply_conviction_bar(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/candidate_ranking.py."""
        return cls._candidate_ranking()._apply_conviction_bar(*args, **kwargs)

    @classmethod
    def _render_candidate_ranking(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/candidate_ranking.py."""
        return cls._candidate_ranking()._render_candidate_ranking(*args, **kwargs)
