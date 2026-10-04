"""How the portfolio-manager seat HOLDS a standalone part instead of inheriting a mixin.

Same shape as `hold_prompt_evidence` (src/agents/portfolio_manager/prompt_evidence.py):
the part is built ONCE, the agent class keeps it under one attribute, and same-named
classmethod delegates keep every existing call site resolving. Two extra pieces the
later parts need:

* `live_body` — a body the part reads through `self.` that tests swap on the AGENT
  class. Handing in the agent's attribute at build time would snapshot it, so the
  collaborator re-reads the class at every call; when the class still carries this
  module's own delegate it runs the held part's own body (the recursion guard the
  per-call shims' `_is_own_shim` used to provide).
* `LiveMapping` — a class-level mapping (the conflict-source aliases) read off the
  agent class at every lookup rather than copied when the part is built.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping


def delegate(holder_attr: str, name: str, bodies_module: str):
    """A classmethod that forwards `name` to the part held under `holder_attr`."""
    def shim(cls, *args, **kwargs):
        return getattr(getattr(cls, holder_attr), name)(*args, **kwargs)
    shim.__name__ = name
    shim.__qualname__ = f"hold.<locals>.{name}"
    shim.__doc__ = f"Thin delegate: body lives in {bodies_module}."
    shim._held_delegate = name
    return classmethod(shim)


def is_delegate(cls, name: str) -> bool:
    """True when `cls` (or a base) still carries the delegate installed for `name`."""
    current = inspect.getattr_static(cls, name, None)
    return getattr(getattr(current, "__func__", current), "_held_delegate", None) == name


def live_body(agent_cls, holder_attr: str, name: str):
    """Collaborator handed in LIVE: read `name` off `agent_cls` at each call."""
    def collaborator(*args, **kwargs):
        if is_delegate(agent_cls, name):
            part = getattr(agent_cls, holder_attr)
            return getattr(type(part), name)(part, *args, **kwargs)
        return getattr(agent_cls, name)(*args, **kwargs)
    collaborator.__name__ = name
    collaborator.__doc__ = f"Live read of `{name}` off the agent class (never snapshotted)."
    return collaborator


class LiveMapping(Mapping):
    """Read-only view of `getattr(host, attr)` taken at every lookup, never at build time."""

    def __init__(self, host, attr: str) -> None:
        self._host = host
        self._attr = attr

    def _current(self):
        return getattr(self._host, self._attr)

    def __getitem__(self, key):
        return self._current()[key]

    def __iter__(self):
        return iter(self._current())

    def __len__(self) -> int:
        return len(self._current())


def hold(agent_cls, *, holder_attr: str, part, delegated, bodies_module: str):
    """Install `part` under `holder_attr` and a delegate for every name in `delegated`."""
    setattr(agent_cls, holder_attr, part)
    for name in delegated:
        setattr(agent_cls, name, delegate(holder_attr, name, bodies_module))
    return agent_cls
