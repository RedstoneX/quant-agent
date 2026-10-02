"""Prompt evidence rows: stance collapsing, sector/macro/earnings rows, the evidence registry.

Bodies live in src/agents/portfolio_manager/evidence_prompting.py (`PromptEvidence`);
this mixin keeps same-named thin shims, built per call so a collaborator swapped
after construction is what the body sees. The module-level names are re-exported
unchanged for importers and for the package's patch mirror.
"""

import inspect

from src.agents.portfolio_manager.evidence_prompting import (  # noqa: F401 — re-exported
    PromptEvidence,
)
from src.cost_circuit.parts.shim_guard import _is_class_shim

#: Bodies the part owns that it also reads through `self.` — handed back to it only
#: when swapped on the host, never as the mixin's own shim (recursion guard).
_OWN_BODIES = ('_collapse_stances', '_earnings_stance_rows', '_macro_sectors', '_macro_stance_rows', '_sector_guidance_rows',)


def _is_own_shim(cls, attr: str) -> bool:
    """`_is_class_shim` sees bound methods and partials; a classmethod read off the
    class binds fresh each time, so the raw descriptor is compared as well."""
    own = vars(PromptEvidenceMixin).get(attr)
    return own is not None and (
        _is_class_shim(getattr(cls, attr, None), attr, PromptEvidenceMixin)
        or inspect.getattr_static(cls, attr, None) is own)


class PromptEvidenceMixin:
    """Prompt evidence rows: stance collapsing, sector/macro/earnings rows, the evidence registry."""

    @classmethod
    def _prompt_evidence(cls) -> PromptEvidence:
        """Thin shim: builds the standalone object from this class's collaborators
        (bodies moved to src/agents/portfolio_manager/evidence_prompting.py). Built per
        call so a collaborator swapped after construction is what the body sees."""
        return PromptEvidence(
            **{
                attr.lstrip("_"): getattr(cls, attr)
                for attr in _OWN_BODIES
                if not _is_own_shim(cls, attr)
            },
        )

    @classmethod
    def _collapse_stances(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._collapse_stances(*args, **kwargs)

    @classmethod
    def _sector_guidance_rows(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._sector_guidance_rows(*args, **kwargs)

    @classmethod
    def _macro_sectors(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._macro_sectors(*args, **kwargs)

    @classmethod
    def _macro_stance_rows(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._macro_stance_rows(*args, **kwargs)

    @classmethod
    def _earnings_stance_rows(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._earnings_stance_rows(*args, **kwargs)

    @classmethod
    def _render_earnings_verdict(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._render_earnings_verdict(*args, **kwargs)

    @classmethod
    def _render_earnings_no_call_rollup(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence()._render_earnings_no_call_rollup(*args, **kwargs)

    @classmethod
    def stale_evidence_sources(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence().stale_evidence_sources(*args, **kwargs)

    @classmethod
    def broadcast_macro_sources(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence().broadcast_macro_sources(*args, **kwargs)

    @classmethod
    def build_evidence_registry(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/evidence_prompting.py."""
        return cls._prompt_evidence().build_evidence_registry(*args, **kwargs)
