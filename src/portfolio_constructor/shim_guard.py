"""Recursion guard for the per-call shims in src/portfolio_constructor (same shape as src/cost_circuit/parts/shim_guard.py)."""

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
