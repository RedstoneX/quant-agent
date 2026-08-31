"""Requests are sized before they are sent, not discovered to be too big.

Owner instruction, 2026-08-31: every request costs money, so the work of
sizing one belongs on our side of the wire. Both obvious alternatives — pick
a rate ceiling and back off when it trips, or let a ceiling learn from
refusals — pay the provider for the lesson. These tests pin the design that
doesn't.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.token_budget import (
    DEFAULT_TOKENS_PER_BYTE,
    MIN_SAMPLES,
    SizeModel,
    pack_to_budget,
    reset_cache,
    size_model,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    reset_cache()


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE agent_logs (agent_name TEXT, model TEXT, "
        "input_message TEXT, input_tokens INTEGER)"
    )
    conn.executemany(
        "INSERT INTO agent_logs VALUES (?,?,?,?)",
        [("a", "m", "x" * b, t) for b, t in rows],
    )
    return conn


# --------------------------------------------------------------- the fit


def _synthetic(fixed, per_byte, sizes):
    return [(b, int(fixed + per_byte * b)) for b in sizes]


def test_the_fit_recovers_the_fixed_overhead_and_the_per_byte_rate():
    """The two parameters are the two real things in a prompt: the system
    prompt, which every request pays once, and the content, which is charged
    by the byte. Recovering both is what lets a batch be packed correctly."""
    conn = _db(_synthetic(4000, 0.94, range(10_000, 200_000, 10_000)))
    model = size_model(conn, "a", "m")
    assert model.measured
    assert model.fixed_tokens == pytest.approx(4000, rel=0.02)
    assert model.tokens_per_byte == pytest.approx(0.94, rel=0.02)


def test_a_single_ratio_could_not_express_this():
    """A bytes-per-token ratio folds the per-request constant into a per-byte
    rate, so it is only right at one message size — and tech_analyst's
    messages span 6KB to 379KB in production."""
    conn = _db(_synthetic(4000, 0.94, [10_000, 200_000]))
    model = size_model(conn, "a", "m")
    small = model.predict(10_000) / 10_000
    large = model.predict(200_000) / 200_000
    assert small > large * 1.3, "cost per byte must not be constant"


def test_too_few_samples_falls_back_rather_than_fitting_noise():
    conn = _db(_synthetic(4000, 0.94, range(1000, 1000 * (MIN_SAMPLES - 1), 1000)))
    model = size_model(conn, "a", "m")
    assert not model.measured
    assert model.tokens_per_byte == DEFAULT_TOKENS_PER_BYTE


def test_samples_that_are_all_the_same_size_cannot_identify_a_slope():
    """Refusing to invent one is the honest move — the data does not contain
    the answer."""
    conn = _db([(50_000, 47_000)] * 20)
    assert not size_model(conn, "a", "m").measured


def test_a_nonsensical_fit_is_rejected():
    """Prompts cannot cost less for being longer. A negative slope means the
    fit is describing noise."""
    conn = _db([(b, 100_000 - b) for b in range(10_000, 90_000, 5_000)])
    assert not size_model(conn, "a", "m").measured


def test_a_broken_database_never_raises_into_a_session():
    """Sizing is an efficiency measure. An efficiency measure that can stop a
    trading session is a bad trade — this whole day was spent removing one."""
    class _Exploding:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table")

    model = size_model(_Exploding(), "a", "m")
    assert not model.measured
    assert model.predict(1000) > 0


def test_extrapolating_far_past_observed_sizes_errs_smaller():
    """Beyond twice anything ever seen the fit is least trustworthy, so the
    estimate inflates — which makes requests smaller, never larger."""
    conn = _db(_synthetic(4000, 0.94, range(10_000, 50_000, 2_000)))
    model = size_model(conn, "a", "m")
    near = model.predict(40_000)
    far = model.predict(400_000)
    assert far > (4000 + 0.94 * 400_000)
    assert near == pytest.approx(4000 + 0.94 * 40_000, rel=0.05)


# --------------------------------------------------------------- packing

_MODEL = SizeModel(
    fixed_tokens=4127, tokens_per_byte=0.939, samples=41,
    measured=True, observed_max_bytes=379_208,
)


def _sizes(batches):
    return [_MODEL.predict(sum(b for _, b in batch)) for batch in batches]


def test_no_request_can_exceed_the_budget():
    """Not 'usually' — structurally. Nothing is ever added to a full batch."""
    items = [(f"S{i}", 3888) for i in range(53)]
    batches = pack_to_budget(items, lambda i: i[1], budget_tokens=45_000, model=_MODEL)
    assert max(_sizes(batches)) <= 45_000


def test_every_item_lands_exactly_once():
    items = [(f"S{i}", 3888) for i in range(53)]
    batches = pack_to_budget(items, lambda i: i[1], budget_tokens=45_000, model=_MODEL)
    flat = [name for batch in batches for name, _ in batch]
    assert sorted(flat) == sorted(name for name, _ in items)


def test_packing_beats_the_fixed_count_it_replaced():
    """The measured comparison this change was made on: 53 symbols, real
    production sizes. Both the peak request AND the total fall — the trimmed
    payload more than pays for the extra repeated system prompts."""
    trimmed = [(f"S{i}", 3888) for i in range(53)]
    untrimmed = [(f"S{i}", 5585) for i in range(53)]

    packed = pack_to_budget(trimmed, lambda i: i[1], budget_tokens=45_000, model=_MODEL)
    fixed = pack_to_budget(
        untrimmed, lambda i: i[1], budget_tokens=10**9, model=_MODEL, max_items=25,
    )
    assert max(_sizes(packed)) < max(_sizes(fixed)) * 0.40   # peak: -60% or better
    assert sum(_sizes(packed)) < sum(_sizes(fixed))          # and fewer tokens overall


def test_an_item_too_large_for_the_whole_budget_still_makes_progress():
    """No empty batch, no infinite loop. One oversized item is a content bug
    worth seeing, not something to silently drop."""
    items = [("huge", 10_000_000), ("small", 100)]
    batches = pack_to_budget(items, lambda i: i[1], budget_tokens=45_000, model=_MODEL)
    flat = [name for batch in batches for name, _ in batch]
    assert flat == ["huge", "small"]


def test_an_unmeasurable_item_is_assumed_large_not_free():
    """The safe direction is fewer items per request."""
    def _explode(item):
        raise ValueError("cannot size this")

    batches = pack_to_budget(
        [("a", 1), ("b", 1)], _explode, budget_tokens=45_000, model=_MODEL,
    )
    assert len(batches) == 2


def test_the_hard_item_cap_still_applies():
    items = [(f"S{i}", 10) for i in range(100)]
    batches = pack_to_budget(
        items, lambda i: i[1], budget_tokens=10**9, model=_MODEL, max_items=30,
    )
    assert max(len(b) for b in batches) == 30


def test_an_empty_batch_packs_to_nothing():
    assert pack_to_budget([], lambda i: i, budget_tokens=1000, model=_MODEL) == []
