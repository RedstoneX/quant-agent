"""Make a backtest sweep PROVE it reached the number it claims to vary.

WHY THIS EXISTS. Four separate times this project read a byte-identical
A/B result as "the parameter is inert" when the truth was "the harness
never reached the parameter". At least one of those values moved real
money materially once it was genuinely wired. A sweep that cannot show a
read count is a trap, because the two outcomes — inert value and
unreached value — are indistinguishable in the numbers.

So: every value this engine lets a sweep vary is read through here, and
every read is COUNTED, per run, at the site that actually uses it. A run
that reports ``0 reads`` for a value has not measured that value; it has
failed to reach it, and says so in its own output.

THE TWO WAYS A VALUE GOES UNREAD, both found in this codebase:

1. ``from X import NAME`` binds the VALUE into the importing module's
   namespace at import time. Reassigning ``X.NAME`` afterwards changes
   nothing the importer sees. ``compute_trailing_stop`` (which lives in
   ``src/risk/trail_evaluate.py``, not in ``src/risk/trailing.py`` where
   its constants are defined) reads its chandelier and noise-band
   multiples this way.

2. ``def f(..., knob=MODULE_CONSTANT)`` evaluates the default ONCE, at
   function-definition time. ``find_structural_levels`` freezes its
   ``pivot_window`` / ``tolerance_pct`` / ``min_touches`` this way, so a
   sweep that reassigns the module constant is swept past silently.

The repairs here are ENGINE-SIDE ONLY and change no value and no trading
rule. For (2) the engine now passes the knobs EXPLICITLY on every call,
read late from the defining module, so a reassignment is honoured and
counted. For (1) the engine installs a counting wrapper of the SAME
numeric value into the namespace that actually reads it, so the count is
taken where the multiplication happens rather than where the constant is
written down.

NOTHING HERE RAISES. A diagnostic that halts a backtest is worse than the
problem it reports. A zero count is reported, never enforced.
"""
from __future__ import annotations

from typing import Any

#: Every value a sweep may vary through this engine, mapped to a one-line
#: description used in the run's own output. A name absent from here is
#: still counted if read, but an unknown name in an override is reported
#: rather than silently applied to nothing.
KNOWN_VALUES: dict[str, str] = {
    "levels.pivot_window": "bars either side of a pivot (src/data/levels.py)",
    "levels.cluster_tolerance_pct": "level cluster width pct (src/data/levels.py)",
    "levels.min_touches": "touches before a level counts (src/data/levels.py)",
    "trailing.chandelier_atr_multiple": "chandelier ATR multiple (src/risk/trailing.py)",
    "trailing.noise_band_atr_multiple": "trail noise-band ATR multiple (src/risk/trailing.py)",
}

_counts: dict[str, int] = {}
_overrides: dict[str, Any] = {}
#: Module attributes this process replaced with a counting wrapper, kept so
#: the originals can be restored exactly.
_installed: dict[tuple[str, str], Any] = {}


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def _bump(name: str) -> None:
    _counts[name] = _counts.get(name, 0) + 1


class CountedFloat(float):
    """A float that is numerically its own value and counts its uses.

    Installed in place of a module constant that was frozen into another
    module by ``from X import NAME``. Every arithmetic use bumps the
    counter and returns a PLAIN float, so nothing downstream can observe
    this type or behave differently because of it.
    """

    __slots__ = ("_swept_name",)

    def __new__(cls, value: float, name: str) -> "CountedFloat":
        obj = super().__new__(cls, value)
        obj._swept_name = name
        return obj

    def _used(self) -> float:
        _bump(self._swept_name)
        return float(self)

    def __mul__(self, other): return self._used() * other
    def __rmul__(self, other): return other * self._used()
    def __add__(self, other): return self._used() + other
    def __radd__(self, other): return other + self._used()
    def __sub__(self, other): return self._used() - other
    def __rsub__(self, other): return other - self._used()
    def __truediv__(self, other): return self._used() / other
    def __rtruediv__(self, other): return other / self._used()
    def __neg__(self): return -self._used()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"CountedFloat({float(self)!r}, {self._swept_name!r})"


def read(name: str, default: Any) -> Any:
    """Return the swept value for `name`, counting this as one read.

    `default` is looked up by the CALLER, late, from the module that
    defines the constant — so reassigning that module attribute is
    honoured even though the consuming function froze its own default.
    """
    _bump(name)
    return _overrides.get(name, default)


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

def set_override(name: str, value: Any) -> None:
    """Set the value a sweep is varying. Unknown names are accepted and
    reported in the output rather than refused — this is a diagnostic."""
    _overrides[name] = value
    _refresh_installed()


def clear_overrides() -> None:
    _overrides.clear()
    _refresh_installed()


def overrides() -> dict[str, Any]:
    return dict(_overrides)


def unknown_overrides() -> list[str]:
    """Override names this engine has no reading site for. A sweep over
    one of these can only ever report zero reads."""
    return sorted(n for n in _overrides if n not in KNOWN_VALUES)


# ---------------------------------------------------------------------------
# Counting wrappers for constants frozen into another module's namespace
# ---------------------------------------------------------------------------

#: (module that READS the constant, attribute name there, swept name,
#:  module that DEFINES it). The reading module is the one that matters:
#: patching the defining module is exactly the mistake this file exists
#: to make visible.
_FROZEN_SITES: tuple[tuple[str, str, str, str], ...] = (
    ("src.risk.trail_evaluate", "CHANDELIER_ATR_MULTIPLE",
     "trailing.chandelier_atr_multiple", "src.risk.trailing"),
    ("src.risk.trail_evaluate", "NOISE_BAND_ATR_MULTIPLE",
     "trailing.noise_band_atr_multiple", "src.risk.trailing"),
)


def install() -> None:
    """Replace each frozen constant with a value-identical counting wrapper
    in the namespace that reads it. Idempotent, and a no-op for any site
    that cannot be imported."""
    import importlib

    for reader, attr, name, definer in _FROZEN_SITES:
        key = (reader, attr)
        if key in _installed:
            continue
        try:
            mod = importlib.import_module(reader)
            current = getattr(mod, attr)
        except Exception:  # pragma: no cover - diagnostic must never break a run
            continue
        if not isinstance(current, float) or isinstance(current, CountedFloat):
            continue
        _installed[key] = current
        value = _overrides.get(name, current)
        setattr(mod, attr, CountedFloat(value, name))
        del definer


def uninstall() -> None:
    """Restore every original constant. Used by tests that must leave the
    live modules exactly as they found them."""
    import importlib

    for (reader, attr), original in list(_installed.items()):
        try:
            setattr(importlib.import_module(reader), attr, original)
        except Exception:  # pragma: no cover
            pass
        del _installed[(reader, attr)]


def _refresh_installed() -> None:
    """Re-apply the wrappers so an override set after `install()` is the
    value the reading module actually holds."""
    import importlib

    for reader, attr, name, _definer in _FROZEN_SITES:
        if (reader, attr) not in _installed:
            continue
        original = _installed[(reader, attr)]
        try:
            mod = importlib.import_module(reader)
        except Exception:  # pragma: no cover
            continue
        setattr(mod, attr, CountedFloat(_overrides.get(name, original), name))


# ---------------------------------------------------------------------------
# Per-run reporting
# ---------------------------------------------------------------------------

def reset_counts() -> None:
    _counts.clear()


def counts() -> dict[str, int]:
    """Read counts for this run: every known value, plus anything else
    that was read. A value present with 0 was NOT reached."""
    out = {name: 0 for name in KNOWN_VALUES}
    out.update(_counts)
    return out


def format_read_counts(label: str = "") -> str:
    """The block a run prints so a zero-read sweep is self-evident.

    A sweep reporting identical numbers AND a zero read count has not
    measured anything; that is an honest failure. Identical numbers with a
    non-zero read count is a real result.
    """
    seen = counts()
    head = f"SWEPT-VALUE READS{(' — ' + label) if label else ''}"
    lines = ["=" * 74, head, "=" * 74,
             "Times this run actually READ each value a sweep can vary.",
             "0 means the run never reached it: any A/B over it is a NON-RESULT,",
             "however different or identical the numbers look.", ""]
    width = max(len(n) for n in seen)
    for name in sorted(seen):
        n = seen[name]
        swept = " [SWEPT]" if name in _overrides else ""
        flag = "" if n else "   <-- NEVER READ"
        lines.append(f"  {name.ljust(width)}  {n:>8,} reads{swept}{flag}")
    unknown = unknown_overrides()
    if unknown:
        lines.append("")
        lines.append("  Overrides with no reading site in this engine (can only "
                     "ever report 0):")
        for name in unknown:
            lines.append(f"    {name}")
    swept_unread = sorted(n for n in _overrides if seen.get(n, 0) == 0)
    lines.append("")
    if swept_unread:
        lines.append("  VERDICT: this sweep did NOT reach " +
                     ", ".join(swept_unread) + " — its result is a non-result.")
    elif _overrides:
        lines.append("  VERDICT: every swept value was read at its use site.")
    else:
        lines.append("  VERDICT: no override set — this is a baseline run.")
    return "\n".join(lines)
