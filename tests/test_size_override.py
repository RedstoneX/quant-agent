"""The override algebra (`src/risk/size_override.py`) — docs/WORK.md item 13.

A target weight of 0% used to mean three incompatible things (refuse / close /
short) and the plumbing could not tell them apart — a rule that refused to BUY
something could silently SELL a position nobody asked to sell. The
2026-09-02 signed-dissent rule worked around this in its one call site
(`PortfolioConstructor._plan_risk_targets`) by dropping a refused target
rather than sizing it at zero. `SizeOverride` generalizes that workaround into
a structural guarantee, borrowed from `pysystemtrade`'s override algebra:

    no_trading  >  close  >  reduce_only  >  (a plain multiplier)

Combining two overrides always yields the more restrictive one. This file
proves three things: (a) the combination function enforces that order for
every pairwise combination, not just a few; (b) `no_trading` absorbs anything
it is combined with; (c) the original bug — a refusal read as a numeric
zero-weight close — is now impossible to express, because `no_trading` (and
`close`, and `reduce_only`) simply have no `.value` to misread. The fourth
property, that the migrated 2026-09-02 caller still behaves the same way, is
covered end-to-end in `tests/test_agreement_sizing.py::
test_a_net_score_of_zero_produces_no_order_at_all`,
`test_a_net_score_below_zero_produces_no_order_at_all`, and
`test_blocking_a_target_leaves_a_held_position_alone` — those three exercise
`PortfolioConstructor.construct_orders` through the now-migrated code path and
were not changed by the migration.
"""

from __future__ import annotations

import itertools

import pytest

from src.risk.size_override import SizeOverride, combine_overrides

_KINDS = ("no_trading", "close", "reduce_only", "multiplier")

# Absorbing order, most restrictive first — the ranking the algebra defines.
_RANK = {"no_trading": 0, "close": 1, "reduce_only": 2, "multiplier": 3}


def _make(kind: str, value: float = 3.0) -> SizeOverride:
    if kind == "no_trading":
        return SizeOverride.no_trading()
    if kind == "close":
        return SizeOverride.close()
    if kind == "reduce_only":
        return SizeOverride.reduce_only()
    return SizeOverride.sized(value)


# ---------------------------------------------------------------------------
# (a) every pairwise combination enforces the absorbing order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind_a,kind_b", list(itertools.product(_KINDS, _KINDS)))
def test_combine_always_yields_the_more_restrictive_kind(kind_a, kind_b):
    a, b = _make(kind_a), _make(kind_b)
    result = a.combine(b)
    expected_kind = kind_a if _RANK[kind_a] <= _RANK[kind_b] else kind_b
    assert result.kind == expected_kind


@pytest.mark.parametrize("kind_a,kind_b", list(itertools.product(_KINDS, _KINDS)))
def test_combine_is_commutative(kind_a, kind_b):
    """The algebra picks the more restrictive operand — order must not matter."""
    a, b = _make(kind_a), _make(kind_b)
    assert a.combine(b).kind == b.combine(a).kind


def test_two_multipliers_combine_to_the_smaller_more_restrictive_value():
    result = SizeOverride.sized(4.0).combine(SizeOverride.sized(1.5))
    assert result.kind == "multiplier"
    assert result.value == 1.5


def test_two_multipliers_combine_regardless_of_argument_order():
    a = SizeOverride.sized(4.0).combine(SizeOverride.sized(1.5))
    b = SizeOverride.sized(1.5).combine(SizeOverride.sized(4.0))
    assert a.value == b.value == 1.5


def test_combine_overrides_reduces_across_more_than_two():
    result = combine_overrides(
        SizeOverride.sized(5.0), SizeOverride.reduce_only(), SizeOverride.sized(2.0),
    )
    assert result.kind == "reduce_only"


def test_combine_overrides_with_no_arguments_is_the_least_restrictive_case():
    result = combine_overrides()
    assert result.kind == "multiplier"
    assert result.value == float("inf")


# ---------------------------------------------------------------------------
# (b) no_trading always wins, whatever it is combined with
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("other_kind", _KINDS)
def test_no_trading_absorbs_everything(other_kind):
    no_trading = SizeOverride.no_trading()
    other = _make(other_kind)
    assert no_trading.combine(other).kind == "no_trading"
    assert other.combine(no_trading).kind == "no_trading"


def test_no_trading_absorbs_an_arbitrarily_large_multiplier():
    """The whole point of the pattern: 'do not trade' cannot be diluted back
    into 'trade a bit' by combining it with something large."""
    result = SizeOverride.no_trading().combine(SizeOverride.sized(1_000_000.0))
    assert result.kind == "no_trading"


# ---------------------------------------------------------------------------
# (c) the original bug is now structurally impossible to express
# ---------------------------------------------------------------------------

def test_refusal_has_no_numeric_value_to_misread_as_a_close():
    """The old bug: a refusal, represented as a bare 0.0 float, was
    indistinguishable from a deliberate zero-weight close. Reading `.value`
    off a `no_trading` override must fail loudly instead of quietly handing
    back a number a delta loop could read as 'close this position'."""
    refusal = SizeOverride.no_trading()
    with pytest.raises(ValueError):
        _ = refusal.value


def test_close_also_has_no_numeric_value():
    """`close` is a real, deliberate instruction (PM's own is_close), not a
    magnitude either — it should not be readable as some particular weight."""
    with pytest.raises(ValueError):
        _ = SizeOverride.close().value


def test_reduce_only_also_has_no_numeric_value():
    with pytest.raises(ValueError):
        _ = SizeOverride.reduce_only().value


def test_no_trading_and_close_are_distinct_kinds():
    """The three original meanings of a zero float are now three different
    values, not one value read three ways."""
    assert SizeOverride.no_trading().kind != SizeOverride.close().kind
    assert SizeOverride.no_trading() != SizeOverride.close()


def test_is_refusal_only_true_for_no_trading():
    assert SizeOverride.no_trading().is_refusal is True
    assert SizeOverride.close().is_refusal is False
    assert SizeOverride.reduce_only().is_refusal is False
    assert SizeOverride.sized(3.0).is_refusal is False


def test_is_tradeable_only_true_for_multiplier():
    assert SizeOverride.sized(3.0).is_tradeable is True
    assert SizeOverride.no_trading().is_tradeable is False
    assert SizeOverride.close().is_tradeable is False
    assert SizeOverride.reduce_only().is_tradeable is False


def test_sized_rejects_a_negative_value():
    """A refusal is `no_trading()`, never a negative multiplier — there is no
    numeric spelling of 'refuse this' left to fall back on."""
    with pytest.raises(ValueError):
        SizeOverride.sized(-1.0)


def test_combining_a_refusal_into_a_request_leaves_no_value_anywhere():
    """End-to-end structural check: even after combining a real request with
    a refusal, there is no `.value` to accidentally read as a size."""
    requested = SizeOverride.sized(5.0)
    refused = SizeOverride.no_trading()
    combined = requested.combine(refused)
    assert combined.kind == "no_trading"
    with pytest.raises(ValueError):
        _ = combined.value
