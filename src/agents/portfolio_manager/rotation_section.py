"""Rotation precheck and the rotation prompt section.

Bodies live in src/agents/portfolio_manager/rotation_rendering.py (`RotationSection`);
this mixin keeps same-named thin shims, built per call so a collaborator swapped
after construction is what the body sees. The module-level names are re-exported
unchanged for importers and for the package's patch mirror.
"""

import inspect

from src.agents.portfolio_manager.rotation_rendering import (  # noqa: F401 — re-exported
    RotationSection,
)
from src.cost_circuit.parts.shim_guard import _is_class_shim

#: Bodies the part owns that it also reads through `self.` — handed back to it only
#: when swapped on the host, never as the mixin's own shim (recursion guard).
_OWN_BODIES = ('_rotation_constraint_line', 'rotation_precheck',)


def _is_own_shim(cls, attr: str) -> bool:
    """`_is_class_shim` sees bound methods and partials; a classmethod read off the
    class binds fresh each time, so the raw descriptor is compared as well."""
    own = vars(RotationSectionMixin).get(attr)
    return own is not None and (
        _is_class_shim(getattr(cls, attr, None), attr, RotationSectionMixin)
        or inspect.getattr_static(cls, attr, None) is own)


class RotationSectionMixin:
    """Rotation precheck and the rotation prompt section."""

    @classmethod
    def _rotation_section(cls) -> RotationSection:
        """Thin shim: builds the standalone object from this class's collaborators
        (bodies moved to src/agents/portfolio_manager/rotation_rendering.py). Built per
        call so a collaborator swapped after construction is what the body sees."""
        return RotationSection(
            **{
                attr.lstrip("_"): getattr(cls, attr)
                for attr in _OWN_BODIES
                if not _is_own_shim(cls, attr)
            },
        )

    @classmethod
    def rotation_precheck(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/rotation_rendering.py."""
        return cls._rotation_section().rotation_precheck(*args, **kwargs)

    @classmethod
    def _rotation_constraint_line(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/rotation_rendering.py."""
        return cls._rotation_section()._rotation_constraint_line(*args, **kwargs)

    @classmethod
    def _render_rotation_section(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/rotation_rendering.py."""
        return cls._rotation_section()._render_rotation_section(*args, **kwargs)
