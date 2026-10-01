"""Shared plumbing for the prompt-fact families (`src/prompt_facts_*.py`).

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210. Each family
class binds only the collaborators its bodies read; a collaborator the owning
pipeline does not have is passed as `_ABSENT` and left UNSET, so
`getattr(self, "config", None)` guards and the AttributeError a body raises on a
missing collaborator behave exactly as they did under the mixin.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")

#: Marks a collaborator the owning pipeline does not have. `PromptFactsMixin`
#: passes it for any attribute missing on a `TradingPipeline` built via
#: `__new__()` (the ~58 such tests), and a family's `__init__` then leaves that
#: name UNSET — so `getattr(self, "config", None)` guards and the AttributeError
#: a body raises on a missing collaborator behave exactly as under the mixin.
_ABSENT = object()


def bind_ports(obj, provided: dict) -> None:
    """Set each provided collaborator on `obj`; leave `_ABSENT` ones unset."""
    for name, value in provided.items():
        if value is not _ABSENT:
            setattr(obj, name, value)
