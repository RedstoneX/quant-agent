"""Rotation section: the agent HOLDS the standalone part instead of inheriting a mixin.

Bodies live in src/agents/portfolio_manager/rotation_rendering.py (`RotationSection`).
`hold_rotation_section(agent_cls)` builds one part and installs same-named classmethod
delegates on the agent class, so every existing call site (`PortfolioManagerAgent.
rotation_precheck(...)`, `self._render_rotation_section(...)`) keeps resolving. The two
bodies the part reads through `self.` are handed in live (see held_part.live_body), so
a swap on the agent class is what the body sees. The module-level names are re-exported
unchanged for the package's patch mirror.
"""

from src.agents.portfolio_manager.rotation_rendering import (  # noqa: F401 — re-exported
    RotationSection,
)
from src.agents.portfolio_manager.held_part import hold, live_body

_HOLDER = "_rotation_section"
_BODIES = "src/agents/portfolio_manager/rotation_rendering.py"

#: Every name the agent exposes for the rotation section, delegated to the held part.
DELEGATED = ('rotation_precheck', '_rotation_constraint_line', '_render_rotation_section',)

#: Bodies the part owns that it also reads through `self.` — handed in live.
LIVE_BODIES = ('_rotation_constraint_line', 'rotation_precheck',)


def build_rotation_section(agent_cls) -> RotationSection:
    """The part, wired to `agent_cls` live; no agent object is constructed."""
    return RotationSection(
        **{attr.lstrip("_"): live_body(agent_cls, _HOLDER, attr) for attr in LIVE_BODIES},
    )


def hold_rotation_section(agent_cls, part: RotationSection | None = None):
    """Make `agent_cls` hold one `RotationSection` and delegate the rotation names to it."""
    part = part if part is not None else build_rotation_section(agent_cls)
    return hold(agent_cls, holder_attr=_HOLDER, part=part, delegated=DELEGATED, bodies_module=_BODIES)
