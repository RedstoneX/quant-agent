"""Recursion guard for the per-call shims in src/cost_circuit/breaker_*.py.

Same approach as `_is_broker_class_shim` in src/execution/broker.py (not
imported from there: src.cost_circuit may not reach the broker seam). A
lifted body passed back into the standalone object as a collaborator would
overwrite that object's own method with a function that calls back into it.
"""

from __future__ import annotations
import functools


def _is_class_shim(obj, attr: str, owner: type) -> bool:
    """True when `obj` is `owner`'s own thin shim for `attr`, however it was
    bound: a bound method (`__func__`), a `functools.partial` over the plain
    function (`func`, unwrapped through nested partials), or the plain function."""
    target = getattr(owner, attr, None)
    if target is None:
        return False
    seen = obj
    for _ in range(8):
        if seen is target:
            return True
        if isinstance(seen, functools.partial):
            seen = seen.func
            continue
        bound = getattr(seen, "__func__", None)
        if bound is None:
            return False
        seen = bound
    return False
