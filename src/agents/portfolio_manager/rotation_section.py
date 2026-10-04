"""Rotation precheck and the rotation prompt section: the agent HOLDS the standalone part.

Bodies live in src/agents/portfolio_manager/rotation_rendering.py (`RotationSection`).
`hold_rotation_section(agent_cls)` builds one part and installs same-named classmethod
delegates on the agent class, so every existing call site (`PortfolioManagerAgent.
rotation_precheck(...)`, `self._render_rotation_section(...)`) keeps resolving. No
collaborator is snapshotted: the part's collaborators are its own two bodies
(`rotation_precheck`, `_rotation_constraint_line`), and nothing reassigns them on the
agent (grep of src and tests, 2026-10-04). The module-level name is re-exported
unchanged for the package's patch mirror.
"""

from src.agents.portfolio_manager.rotation_rendering import (  # noqa: F401 — re-exported
    RotationSection,
)

#: Every name the agent exposes for the rotation section, delegated to the held part.
DELEGATED = (
    'rotation_precheck', '_rotation_constraint_line', '_render_rotation_section',
)


def _delegate(name: str):
    def shim(cls, *args, **kwargs):
        return getattr(cls._rotation_section, name)(*args, **kwargs)
    shim.__name__ = name
    shim.__qualname__ = f"hold_rotation_section.<locals>.{name}"
    shim.__doc__ = "Thin delegate: body lives in src/agents/portfolio_manager/rotation_rendering.py."
    return classmethod(shim)


def hold_rotation_section(agent_cls, part: RotationSection | None = None):
    """Make `agent_cls` hold one `RotationSection` and delegate the rotation names to it."""
    agent_cls._rotation_section = part if part is not None else RotationSection()
    for name in DELEGATED:
        setattr(agent_cls, name, _delegate(name))
    return agent_cls
