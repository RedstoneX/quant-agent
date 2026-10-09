"""Usd, Price and Qty keep their kind through arithmetic and refuse to mix."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.money import Price, Qty, Usd

KINDS = (Usd, Price, Qty)


@pytest.mark.parametrize("kind", KINDS)
def test_constructs_from_decimal_int_and_str_but_not_float(kind):
    assert kind(Decimal("1.5")).value == Decimal("1.5")
    assert kind(2).value == Decimal(2)
    assert kind("0.10").value == Decimal("0.10")
    with pytest.raises(TypeError):
        kind(1.5)
    with pytest.raises(TypeError):
        kind(True)


@pytest.mark.parametrize("kind", KINDS)
def test_same_kind_arithmetic_keeps_the_kind(kind):
    a, b = kind("3"), kind("1.5")
    assert a + b == kind("4.5")
    assert a - b == kind("1.5")
    assert a * 2 == kind("6") and 2 * a == kind("6")
    assert a * Decimal("0.5") == kind("1.5")
    assert a / 2 == kind("1.5")
    assert -a == kind("-3") and abs(-a) == a
    for result in (a + b, a - b, a * 2, a / 2, -a):
        assert type(result) is kind


@pytest.mark.parametrize("kind", KINDS)
def test_compare_hash_truth_and_repr(kind):
    assert kind("1") < kind("2") <= kind("2") == kind("2.0")
    assert hash(kind("1")) == hash(kind("1.0"))
    assert not kind("0") and kind("0.01")
    assert repr(kind("1.25")) == f"{kind.__name__}('1.25')"
    with pytest.raises(AttributeError):
        kind("1").value = Decimal("9")  # frozen


@pytest.mark.parametrize("left", KINDS)
@pytest.mark.parametrize("right", KINDS)
def test_adding_or_ordering_different_kinds_is_a_type_error(left, right):
    if left is right:
        return
    with pytest.raises(TypeError):
        left("1") + right("1")
    with pytest.raises(TypeError):
        left("1") - right("1")
    with pytest.raises(TypeError):
        left("1") < right("1")
    assert left("1") != right("1")


@pytest.mark.parametrize("kind", KINDS)
def test_raw_decimal_does_not_add_to_a_kind_and_bool_is_not_a_scalar(kind):
    with pytest.raises(TypeError):
        kind("1") + Decimal("1")
    with pytest.raises(TypeError):
        Decimal("1") + kind("1")
    with pytest.raises(TypeError):
        kind("1") * 1.5
    with pytest.raises(TypeError):
        kind("1") * True


def test_price_times_qty_is_usd_and_the_inverses_hold():
    cost = Price("10.50") * Qty("4")
    assert cost == Usd("42.00") and type(cost) is Usd
    assert Qty("4") * Price("10.50") == Usd("42.00")
    per_share = Usd("42") / Qty("4")
    assert per_share == Price("10.5") and type(per_share) is Price
    shares = Usd("42") / Price("10.5")
    assert shares == Qty("4") and type(shares) is Qty


def test_usd_times_usd_and_other_nonsense_products_are_refused():
    with pytest.raises(TypeError):
        Usd("1") * Usd("1")
    with pytest.raises(TypeError):
        Usd("1") * Price("1")
    with pytest.raises(TypeError):
        Price("1") * Price("1")
    with pytest.raises(TypeError):
        Qty("1") * Qty("1")
    with pytest.raises(TypeError):
        Price("1") / Qty("1")
    with pytest.raises(TypeError):
        Qty("1") / Price("1")
    with pytest.raises(TypeError):
        Usd("1") / Usd("1")
