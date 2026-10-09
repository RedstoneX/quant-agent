"""Prompt evidence rows: the agent HOLDS the standalone part instead of inheriting a mixin.

Bodies live in src/agents/portfolio_manager/evidence_prompting.py (`PromptEvidence`).
`hold_prompt_evidence(agent_cls)` builds one part and installs same-named classmethod
delegates on the agent class, so every existing call site (`PortfolioManagerAgent.
build_evidence_registry(...)`, `self._collapse_stances(...)`) keeps resolving. No
collaborator is snapshotted: the part's collaborators are its own bodies, and nothing
reassigns them on the agent. The module-level names are re-exported unchanged for the
package's patch mirror.
"""

from src.agents.portfolio_manager.evidence_prompting import (  # noqa: F401 — re-exported
    PromptEvidence,
)

#: Every name the agent exposes for the prompt-evidence rows, delegated to the held part.
DELEGATED = (
    "_collapse_stances",
    "_sector_guidance_rows",
    "_macro_sectors",
    "_macro_stance_rows",
    "_earnings_stance_rows",
    "_render_earnings_verdict",
    "_render_earnings_no_call_rollup",
    "stale_evidence_sources",
    "broadcast_macro_sources",
    "build_evidence_registry",
)


def _delegate(name: str):
    def shim(cls, *args, **kwargs):
        return getattr(cls._prompt_evidence, name)(*args, **kwargs)

    shim.__name__ = name
    shim.__qualname__ = f"hold_prompt_evidence.<locals>.{name}"
    shim.__doc__ = "Thin delegate: body lives in src/agents/portfolio_manager/evidence_prompting.py."
    return classmethod(shim)


def hold_prompt_evidence(agent_cls, part: PromptEvidence | None = None):
    """Make `agent_cls` hold one `PromptEvidence` and delegate the evidence names to it."""
    agent_cls._prompt_evidence = part if part is not None else PromptEvidence()
    for name in DELEGATED:
        setattr(agent_cls, name, _delegate(name))
    return agent_cls
