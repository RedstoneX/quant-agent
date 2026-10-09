"""Money types: dollars, a per-share price and a share count that cannot be mixed by accident.

Each is a small frozen class wrapping ``Decimal``. They are classes, not ``NewType``
aliases, because a ``NewType`` evaporates on the first arithmetic operation: adding
two ``NewType('Usd', Decimal)`` values gives a plain ``Decimal``, so a checker can no
longer tell dollars from shares two lines later. These classes keep their kind through
arithmetic and refuse, at runtime and to a type checker, to combine with the wrong kind:

    Usd + Usd -> Usd        Price * Qty -> Usd       Usd / Qty -> Price
    Usd + Price -> TypeError                         Usd / Price -> Qty
    Usd * scalar -> Usd     Qty * scalar -> Qty      Price * scalar -> Price

A float is rejected everywhere (it is lossy); construct from ``Decimal``, ``int`` or
``str``. The values are the raw ``Decimal`` under ``.value`` for the boundary where
money leaves the type system (the broker SDK, a journal row). The money-code scope
check (``scripts/money_modules.py``) will use these types, not a name heuristic, to
decide which modules are money paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Self, Union

Scalar = Union[Decimal, int]
Raw = Union[Decimal, int, str]


def _to_decimal(raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int, str)):
        raise TypeError(f"money types take Decimal, int or str, not {type(raw).__name__}")
    return Decimal(raw)


def _scalar(other: object) -> Decimal | None:
    """The scaling factor when ``other`` is a bare number, else None (so the op is refused)."""
    if isinstance(other, bool) or not isinstance(other, (Decimal, int)):
        return None
    return Decimal(other)


@dataclass(frozen=True, slots=True, order=True, init=False, repr=False)
class _Quantity:
    """Shared behaviour: same-kind add/sub/compare, scalar multiply/divide, sign, truth."""

    value: Decimal

    def __init__(self, raw: Raw) -> None:
        object.__setattr__(self, "value", _to_decimal(raw))

    def _same(self, raw: Decimal) -> Self:
        return type(self)(raw)

    def __add__(self, other: object) -> Self:
        if type(other) is not type(self):
            return NotImplemented
        return self._same(self.value + other.value)

    def __sub__(self, other: object) -> Self:
        if type(other) is not type(self):
            return NotImplemented
        return self._same(self.value - other.value)

    def __mul__(self, other: object) -> Self:
        k = _scalar(other)
        return NotImplemented if k is None else self._same(self.value * k)

    __rmul__ = __mul__

    def __truediv__(self, other: object) -> Self:
        k = _scalar(other)
        return NotImplemented if k is None else self._same(self.value / k)

    def __neg__(self) -> Self:
        return self._same(-self.value)

    def __abs__(self) -> Self:
        return self._same(abs(self.value))

    def __bool__(self) -> bool:
        return bool(self.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.value)!r})"


class Usd(_Quantity):
    """Dollars. ``Usd / Qty`` is a per-share ``Price``; ``Usd / Price`` is a share ``Qty``."""

    def __truediv__(self, other: object) -> Price | Qty | Usd:
        if isinstance(other, Qty):
            return Price(self.value / other.value)
        if isinstance(other, Price):
            return Qty(self.value / other.value)
        return super().__truediv__(other)


class Price(_Quantity):
    """Dollars per share. ``Price * Qty`` is ``Usd``."""

    def __mul__(self, other: object) -> Usd | Price:
        if isinstance(other, Qty):
            return Usd(self.value * other.value)
        return super().__mul__(other)

    __rmul__ = __mul__


class Qty(_Quantity):
    """A share count. ``Qty * Price`` is ``Usd``."""

    def __mul__(self, other: object) -> Usd | Qty:
        if isinstance(other, Price):
            return Usd(self.value * other.value)
        return super().__mul__(other)

    __rmul__ = __mul__


__all__ = ["Price", "Qty", "Usd"]
