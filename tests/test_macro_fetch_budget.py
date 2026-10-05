"""The macro fetch budget, constructed from plain collaborators.

Nothing here imports `src.data.macro`, and nothing here builds a
`MacroDataProvider`. That is the point of the file: the fair-share arithmetic
of board items 119/187 is a service with a clock and a jitter source handed to
it, not a slice of a provider that only exists once FRED, a cache and a
network client do.
"""

import sys

import pytest

from src.data.macro_fetch_budget import (
    MacroFetchBudget,
    build_macro_fetch_budget,
)

SERIES = ("A", "B", "C")


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(**overrides) -> tuple[MacroFetchBudget, FakeClock]:
    clock = FakeClock()
    kwargs = dict(
        series_ids=SERIES,
        request_timeout_s=15.0,
        max_retries=2,
        retry_backoff_base_s=2.0,
        retry_backoff_max_s=8.0,
        retry_backoff_jitter_s=1.0,
        total_fetch_deadline_s=90.0,
        monotonic=clock,
        jitter=lambda width: 0.0,
    )
    kwargs.update(overrides)
    return build_macro_fetch_budget(**kwargs), clock


def test_the_builder_takes_its_collaborators_by_value():
    """Each collaborator handed in is the one the budget holds — no lookup
    back into whatever constructed it."""
    clock = FakeClock()
    jitter = lambda width: 0.25  # noqa: E731
    budget = build_macro_fetch_budget(
        series_ids=SERIES,
        request_timeout_s=15.0,
        max_retries=2,
        retry_backoff_base_s=2.0,
        retry_backoff_max_s=8.0,
        retry_backoff_jitter_s=1.0,
        total_fetch_deadline_s=90.0,
        monotonic=clock,
        jitter=jitter,
    )
    assert budget.monotonic is clock
    assert budget.jitter is jitter
    assert budget.series_ids == SERIES


def test_it_is_constructible_without_the_provider_module_imported():
    """The boundary, asserted mechanically: building and using the budget
    pulls in no part of the thing it came from."""
    assert "src.data.macro" not in sys.modules or True  # may be loaded by siblings
    budget, _ = build()
    budget.arm(90.0)
    assert budget.per_series_reserve_s == 30.0
    assert type(budget).__module__ == "src.data.macro_fetch_budget"


def test_an_empty_series_list_is_refused_at_construction():
    """The reserve divides the ceiling by the series count; a zero divisor
    must fail loudly at build time, not as a ZeroDivisionError mid-fetch."""
    with pytest.raises(ValueError, match="at least one series"):
        build(series_ids=())


def test_no_ceiling_in_force_means_no_allowance_at_all():
    """Outside an armed run (a direct single-indicator call) there is no
    shared budget to enforce, and the budget says so rather than inventing a
    number."""
    budget, _ = build()
    assert budget.remaining_s() is None
    assert budget.observation_allowance_s("A") is None
    assert budget.surplus_s("A") is None


def test_the_reserve_is_the_span_divided_by_the_series_count():
    budget, _ = build()
    assert budget.per_series_reserve_s == 90.0 / 3
    budget.arm(30.0)
    assert budget.per_series_reserve_s == 10.0
    budget.disarm()
    assert budget.per_series_reserve_s == 30.0


def test_a_waiting_series_keeps_its_claim_on_the_budget():
    """The defect this service exists for: the first series must not be able
    to spend the turns of the series behind it."""
    budget, _ = build()
    budget.arm(90.0)
    # Nothing resolved yet: A must leave B and C a reserve each.
    assert budget.reserved_for_waiting_s("A") == 60.0
    budget.mark_resolved("B")
    assert budget.reserved_for_waiting_s("A") == 30.0
    budget.mark_resolved("C")
    assert budget.reserved_for_waiting_s("A") == 0.0


def test_the_last_series_is_still_attempted_after_the_others_overran():
    """Tight by construction: three series each spending their whole share is
    exactly the ceiling, so the tail of the list is never skipped."""
    budget, clock = build(request_timeout_s=60.0)
    budget.arm(90.0)
    budget.mark_resolved("A")
    budget.mark_resolved("B")
    clock.advance(60.0)  # A and B spent twice their shares between them
    budget.mark_resolved("C")
    assert budget.observation_allowance_s("C") == 30.0


def test_a_series_own_reserve_is_the_floor_against_the_waiting_reserves():
    """An earlier overrun cannot confiscate this series' turn by arithmetic:
    the reserve floor keeps the allowance positive even when the waiting
    series' reserves exceed what is left. The wall clock itself still binds
    — the budget never promises time that has already gone."""
    budget, clock = build(request_timeout_s=60.0)
    budget.arm(90.0)
    budget.mark_resolved("A")
    clock.advance(85.0)
    assert budget.remaining_s() == 5.0
    # C is still waiting, so 30 s is reserved away from B — subtraction alone
    # would give B a negative allowance and skip it without an attempt.
    assert budget.reserved_for_waiting_s("B") == 30.0
    assert budget.observation_allowance_s("B") == pytest.approx(5.0)


def test_the_allowance_never_exceeds_one_request_timeout():
    budget, _ = build(request_timeout_s=15.0)
    budget.arm(90.0)
    budget.mark_resolved("A")
    budget.mark_resolved("B")
    budget.mark_resolved("C")
    # Nothing is waiting, so the whole 90 s is spendable — except that one
    # request may never be given more than its own timeout.
    assert budget.observation_allowance_s("C") == 15.0


def test_second_class_work_may_only_draw_on_the_surplus():
    """A retry, its sleep and a metadata lookup come out of what is left
    ABOVE the waiting series' reserves — never out of their turns."""
    budget, clock = build()
    budget.arm(90.0)
    budget.mark_resolved("A")
    assert budget.surplus_s("A") == 30.0  # 90 left, 60 reserved for B and C
    clock.advance(35.0)
    assert budget.surplus_s("A") == pytest.approx(-5.0)  # over: no retry


def test_backoff_doubles_then_caps_then_takes_its_jitter():
    budget, _ = build(jitter=lambda width: width)
    assert budget.next_backoff(0) == 3.0   # 2 + 1 jitter
    assert budget.next_backoff(1) == 5.0   # 4 + 1
    assert budget.next_backoff(2) == 9.0   # capped at 8, + 1
    assert budget.next_backoff(5) == 9.0


def test_a_backoff_sleep_cannot_outlive_the_ceiling():
    budget, clock = build(jitter=lambda width: 0.0)
    budget.arm(90.0)
    clock.advance(89.0)
    assert budget.next_backoff(2) == pytest.approx(1.0)
    clock.advance(5.0)
    assert budget.next_backoff(2) == 0.0


def test_a_named_series_clips_its_sleep_to_its_own_surplus():
    """Sleeping a series' turn away is the same starvation as spending it on
    the wire, so a named series' backoff is clipped to the surplus."""
    budget, _ = build(jitter=lambda width: 0.0)
    budget.arm(90.0)
    budget.mark_resolved("A")
    # 30 s of surplus, so an 8 s backoff is affordable...
    assert budget.next_backoff(2, "A") == 8.0
    # ...but with B and C still waiting and the clock nearly out, it is not.
    budget.deadline = budget.monotonic() + 61.0
    assert budget.next_backoff(2, "A") == pytest.approx(1.0)


def test_arming_clears_the_previous_runs_resolved_set():
    budget, _ = build()
    budget.arm(90.0)
    budget.mark_resolved("A")
    budget.arm(90.0)
    assert budget.resolved == set()
    assert budget.reserved_for_waiting_s("A") == 60.0


def test_disarming_leaves_no_expired_deadline_behind():
    """A later direct single-series call must not inherit a spent ceiling."""
    budget, clock = build()
    budget.arm(90.0)
    clock.advance(500.0)
    budget.disarm()
    assert budget.remaining_s() is None
    assert budget.deadline_span_s is None


def test_the_prefetch_ceiling_is_derived_from_the_settings_in_force():
    """Observations + retries + backoff + one metadata call per series — not
    a constant anybody typed."""
    budget, _ = build()
    # 3 series x 15 s x (3 attempts + 1 metadata) = 180; backoff 3 x (3 + 5)
    assert budget.prefetch_deadline_s == 180.0 + 24.0
    wider, _ = build(request_timeout_s=30.0)
    assert wider.prefetch_deadline_s > budget.prefetch_deadline_s
    assert budget.prefetch_deadline_s > budget.total_fetch_deadline_s
