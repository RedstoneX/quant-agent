"""Soft-exit vocabulary shared by the model base and the seat heal.

Lives below both `src.models.base` (which coerces the fields) and
`src.seat_heal` (which restores a stated string a wipe dropped), so
neither has to import the other. Values and behaviour are unchanged.
"""

# Recordable "don't know" for a soft-exit field (`thesis_invalid_if` /
# `catalyst`) when the model sent JSON null on an actionable call. Not a
# falsifier and not a catalyst — `check_thesis_invalid_if` treats it as
# UNPARSEABLE, the same as empty. Empty remains the correct value on a
# neutral Tech read (the prompt says leave it empty). Distinct from silent
# schema-default "" so Risk can see "the seat said it does not know"
# instead of "the field was wiped".
SOFT_EXIT_UNKNOWN = "unknown"
_SOFT_EXIT_FIELDS = frozenset({"thesis_invalid_if", "catalyst"})
# Durable refuse-before-book reason after mechanical heal + one paid retry
# still left an open name without a real "I'll sell if". Not invented
# prose and not a catalyst. The #432 isolate-unknown-only gate is the
# TEMPORARY last-resort that records this reason; delete that isolate
# when a live session proves no actionable name arrives blank.
SOFT_EXIT_MISSING_AFTER_RETRY = "soft-exit missing after retry"
# Durable per-name record of what the soft-exit heal ACTUALLY did before
# that refusal could be reached — filled on the one paid retry, blocked by
# the spend cap, never attempted, errored, or re-asked and still blank.
# Board item 78: without it, the refusal above asserts a retry that may
# never have run, and a heal that quietly did nothing leaves no trace.
SOFT_EXIT_HEAL_EVENT_REASON = "soft_exit_heal"
def stated_soft_exit(value: str | None) -> str:
    """A checkable falsifier/catalyst, or empty.

    `unknown` is the recordable don't-know token, not a condition. Callers
    that need a checkable string (hard-stop substitution, constructor
    parenthetical) treat it as absent. The raw field stays `unknown` so
    Risk can see the seat said it does not know.
    """
    text = (value or "").strip()
    if not text or text.lower() == SOFT_EXIT_UNKNOWN:
        return ""
    return text


def missing_stated_falsifier(value: str | None) -> bool:
    """True when there is no checkable 'I'll sell if' string.

    Empty, whitespace, and the recordable don't-know token `unknown` are
    all missing. Neutral Tech may omit; an actionable rating and an
    open/increase target may not enter the ticket book in this state.
    A reduction or close may omit the field — that omit does not make
    PM thesis free text a sell warrant.
    """
    return not stated_soft_exit(value)
