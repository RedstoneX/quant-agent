"""Keep a seat's stated falsifier alive across a later blank write.

Board item 78, first box. The falsifier is the seat's own 'I'll sell if'
sentence. The desk refuses to enter a name without one, so a later write
that blanks it silently removes the exit condition while the position
stays open. `LLMOutputModel`'s null-wipe heal cannot see this case: on
assignment pydantic hands the validator only the NEW value, so the old
sentence is already gone (measured: assigning None to a stated falsifier
read back `unknown`, assigning whitespace read back whitespace).

The only source this ever restores from is the sentence the model already
held. Nothing is invented. A blank write with nothing to restore is left
exactly as the existing coercion renders it and is counted as genuinely
absent. Counted in `soft_exit_heal_restores` (parked observations drained
once per run):
  present  -> blank_found=False (the model's own validator row)
  healed   -> blank_found=True, healed=True, source `prior_assigned_value`
              (this module's row)
  absent   -> blank_found=True, healed=False, source None (the model's own
              validator row, which already fires on the blank write; a
              second row here would double-count it)
(the construction-time heal uses source `raw_model_output`, so the two heal
routes stay distinguishable.)
"""

from src.models.base import stated_soft_exit

FALSIFIER = "thesis_invalid_if"
PRIOR_SOURCE = "prior_assigned_value"


def install(cls) -> None:
    """Wrap `cls.__setattr__` so a blank write cannot erase a stated falsifier."""
    original_setattr = cls.__setattr__

    def __setattr__(self, name, value):
        if name == FALSIFIER and not stated_soft_exit(value if isinstance(value, str) else None):
            prior = stated_soft_exit(self.__dict__.get(FALSIFIER))
            if prior:
                from src.seat_heal import _note_restore_observation

                sym = getattr(self, "symbol", None)
                sym = sym.strip().upper() if isinstance(sym, str) and sym.strip() else None
                _note_restore_observation(
                    {
                        "symbol": sym,
                        "blank_found": True,
                        "healed": True,
                        "source": PRIOR_SOURCE,
                    }
                )
            if prior:
                value = prior
        original_setattr(self, name, value)

    cls.__setattr__ = __setattr__
