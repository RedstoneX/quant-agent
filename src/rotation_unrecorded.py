"""Board item 219 - a pruning record with no examined count is not an empty book.

`precheck_record` began writing `held_examined_count` after the pass had
already run for a week. A stored record from before that carries no count at
all, and the reader used to coerce the absent field to zero and tell the owner
"the book is empty, so nothing could be cut" - an untrue sentence that looks
like a clean pass. Absent means NOT RECORDED; only a written zero means empty.
"""
from __future__ import annotations


def examined_count_of(record: dict | None) -> int | None:
    """The recorded examined count, or None when the record never held one."""
    if not isinstance(record, dict):
        return None
    value = record.get("held_examined_count")
    if value is None:
        return None
    return int(value)


def empty_pass_lines(record: dict | None) -> list[str]:
    """The opening line for a pass whose examined count reads as zero."""
    if examined_count_of(record) is None:
        return [
            "✂️ Pruning pass: ran, but this stored record predates "
            "the examined-holdings field, so what it examined was not "
            "recorded — it does not say the book was empty."
        ]
    return [
        "✂️ Pruning pass: ran, and there were no holdings to "
        "examine — the book is empty, so nothing could be cut."
    ]
