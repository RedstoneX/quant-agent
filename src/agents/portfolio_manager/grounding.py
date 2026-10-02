"""Decision grounding: validation of the model reply, conflict/catalyst/rejection/target drops, canonical targets.

Bodies live in src/agents/portfolio_manager/decision_grounding.py (`DecisionGrounding`);
this mixin keeps same-named thin shims, built per call so a collaborator swapped
after construction is what the body sees. The module-level names are re-exported
unchanged for importers and for the package's patch mirror.
"""

import inspect

from src.agents.portfolio_manager.decision_grounding import (  # noqa: F401 — re-exported
    CONFLICT_UNADJUDICATED_STATUS,
    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
    DecisionGrounding,
    _ISO_DATE_RE,
    _STATE_CHANGE_ROW_RE,
    _SYMBOL_DIRECTION_RE,
    logger,
)
from src.cost_circuit.parts.shim_guard import _is_class_shim

#: Bodies the part owns that it also reads through `self.` — handed back to it only
#: when swapped on the host, never as the mixin's own shim (recursion guard).
_OWN_BODIES = ("_target_intent", "_canonical_targets", "_conflict_is_named")


def _is_own_shim(cls, attr: str) -> bool:
    """`_is_class_shim` sees bound methods and partials; a classmethod read off the
    class binds fresh each time, so the raw descriptor is compared as well."""
    own = vars(DecisionGroundingMixin).get(attr)
    return own is not None and (
        _is_class_shim(getattr(cls, attr, None), attr, DecisionGroundingMixin)
        or inspect.getattr_static(cls, attr, None) is own)


class DecisionGroundingMixin:
    """Decision grounding: validation of the model reply, conflict/catalyst/rejection/target drops, canonical targets."""

    # §9.3 "disagreement must be adjudicated" ------------------------------
    #
    # `source` values that need a plainer English alias to be recognised in
    # free-form prose. The four other sources (technical/news/earnings/
    # macro) are themselves ordinary words; `smart_money` is normally
    # written "smart money" by a model composing a sentence, so it is
    # aliased explicitly rather than guessed at by a second rule.
    _CONFLICT_SOURCE_ALIASES = {
        "smart_money": ("smart_money", "smart money", "smart-money"),
    }

    _DECISION_FIELDS = ("targets",)

    @classmethod
    def _grounding(cls) -> DecisionGrounding:
        """Thin shim: builds the standalone object from this class's collaborators
        (bodies moved to src/agents/portfolio_manager/decision_grounding.py). Built per
        call so a collaborator swapped after construction is what the body sees."""
        return DecisionGrounding(
            build_evidence_registry=cls.build_evidence_registry,
            conflict_source_aliases=cls._CONFLICT_SOURCE_ALIASES,
            **{
                attr.lstrip("_"): getattr(cls, attr)
                for attr in _OWN_BODIES
                if not _is_own_shim(cls, attr)
            },
        )

    @classmethod
    def _target_intent(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._target_intent(*args, **kwargs)

    @classmethod
    def validate_grounding(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding().validate_grounding(*args, **kwargs)

    @classmethod
    def _conflict_is_named(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._conflict_is_named(*args, **kwargs)

    @classmethod
    def _drop_unadjudicated_conflicts(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._drop_unadjudicated_conflicts(*args, **kwargs)

    @classmethod
    def _state_change_symbols_by_date(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._state_change_symbols_by_date(*args, **kwargs)

    @classmethod
    def _catalyst_cites_state_change(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._catalyst_cites_state_change(*args, **kwargs)

    @classmethod
    def _apply_subfloor_catalyst_rule(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._apply_subfloor_catalyst_rule(*args, **kwargs)

    @classmethod
    def _drop_invalid_rejections(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._drop_invalid_rejections(*args, **kwargs)

    @classmethod
    def _drop_invalid_targets(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._drop_invalid_targets(*args, **kwargs)

    @classmethod
    def _canonical_targets(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._canonical_targets(*args, **kwargs)

    @classmethod
    def _decision_fields_unchanged(cls, *args, **kwargs):
        """Thin shim: body moved to src/agents/portfolio_manager/decision_grounding.py."""
        return cls._grounding()._decision_fields_unchanged(*args, **kwargs)
