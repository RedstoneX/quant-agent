"""Fault injection: the rehearsal harness must be able to make a provider fail.

Every recorded model response the harness replays is, by construction, a
response that SUCCEEDED. Before `ops/rehearsal/faults.py` existed, the retry
loop, the cross-provider failover, and every cost-circuit guard those cross
could not be exercised offline at all — they were reachable only by waiting
for a real provider to fail during a real trading session.

That is not a theoretical gap. It is why a weekend of testing and auditing
passed clean over a defect that made cross-provider failover impossible to
complete, and why the market found it instead at 09:32 on the Monday. See
`tests/test_provider_attempt_budget.py` for the defect itself.
"""

from __future__ import annotations

import threading

import pytest

from ops.rehearsal.faults import (
    KINDS,
    InjectedProviderFault,
    ProviderFaultInjector,
    parse_spec,
)
from src.agents.base import _is_retryable


# ------------------------------------------------------------ spec parsing


def test_spec_with_a_count():
    spec = parse_spec("tech_analyst:rate_limit:2")
    assert (spec.agent, spec.kind, spec.count) == ("tech_analyst", "rate_limit", 2)


def test_spec_without_a_count_means_every_attempt():
    assert parse_spec("tech_analyst:rate_limit").count is None


def test_wildcard_agent_matches_everything():
    spec = parse_spec("*:timeout")
    assert spec.matches("tech_analyst") and spec.matches("portfolio_manager")


@pytest.mark.parametrize("bad", [
    "tech_analyst", "tech_analyst:", ":rate_limit", "a:not_a_kind",
    "a:rate_limit:0", "a:rate_limit:-1", "a:rate_limit:x", "a:b:c:d",
])
def test_bad_specs_are_rejected_with_an_explanation(bad):
    with pytest.raises(ValueError):
        parse_spec(bad)


# ------------------------------------------------- retryability is not ours

# The production classifier decides what happens after an injected failure,
# exactly as it does for a real one. Pinning each kind here means that if
# `_is_retryable` is ever tightened, THIS fails loudly rather than the faults
# quietly ceasing to represent the failures they are named after.

@pytest.mark.parametrize("kind,retryable", [
    ("rate_limit", True),          # 429 — the 2026-08-31 failure
    ("server_error", True),        # 503 — upstream outage
    ("timeout", True),             # no status — unclassified transient
    ("auth", False),               # 401 — a dead key cannot be slept off
    ("insufficient_balance", False),  # 402 — the DeepSeek out-of-money case
])
def test_fault_kinds_are_classified_as_intended(kind, retryable):
    assert _is_retryable(KINDS[kind]("injected")) is retryable


def test_every_kind_is_pinned_by_the_test_above():
    """A new fault kind must arrive with its classification asserted, not
    inherit whatever the catch-all happens to do that week."""
    pinned = {
        "rate_limit", "server_error", "timeout", "auth", "insufficient_balance",
    }
    assert set(KINDS) == pinned


# ---------------------------------------------------------------- counting


def test_the_count_is_spent_then_calls_pass_through():
    injector = ProviderFaultInjector.from_specs(["tech_analyst:rate_limit:2"])
    for attempt in (1, 2):
        with pytest.raises(InjectedProviderFault):
            injector.check("tech_analyst", "google/gemini-2.5-flash-lite")
    injector.check("tech_analyst", "claude-opus-4-7")   # the failover gets through
    assert len(injector.injected) == 2


def test_a_countless_spec_never_relents():
    injector = ProviderFaultInjector.from_specs(["tech_analyst:server_error"])
    for _ in range(5):
        with pytest.raises(InjectedProviderFault):
            injector.check("tech_analyst", "m")


def test_faults_are_scoped_to_the_named_agent():
    injector = ProviderFaultInjector.from_specs(["tech_analyst:rate_limit"])
    with pytest.raises(InjectedProviderFault):
        injector.check("tech_analyst", "m")
    injector.check("portfolio_manager", "m")


def test_no_specs_means_no_interference():
    injector = ProviderFaultInjector.from_specs([])
    assert not injector.active
    for _ in range(3):
        injector.check("tech_analyst", "m")
    assert injector.injected == []


def test_the_fault_budget_is_not_double_spent_under_concurrency():
    """Specialists run in parallel. A budget two threads could both claim
    would make a rehearsal non-deterministic — the one property it exists to
    have."""
    injector = ProviderFaultInjector.from_specs(["*:rate_limit:10"])
    errors: list[Exception] = []
    passed: list[int] = []

    def hammer():
        for _ in range(10):
            try:
                injector.check("tech_analyst", "m")
                passed.append(1)
            except InjectedProviderFault as exc:
                errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 10, "exactly the budgeted attempts must fail"
    assert len(passed) == 30
    assert len({id(e) for e in errors}) == 10


def test_each_fault_carries_its_own_report_record():
    """Read by the replay patch under concurrency — looking the record up
    from `injected[-1]` afterwards can pick up another thread's."""
    injector = ProviderFaultInjector.from_specs(["tech_analyst:auth:1"])
    with pytest.raises(InjectedProviderFault) as excinfo:
        injector.check("tech_analyst", "google/gemini-2.5-flash-lite")
    record = excinfo.value.record
    assert record["kind"] == "injected_provider_fault"
    assert record["agent"] == "tech_analyst"
    assert record["fault_kind"] == "auth"
    assert record["attempt"] == 1
    assert "FORCED" in record["detail"]


def test_an_injected_fault_is_never_mistaken_for_a_real_one():
    """A fault-injected rehearsal describes a hypothetical. Its findings must
    say so in words an operator reads, not only in a field name."""
    injector = ProviderFaultInjector.from_specs(["tech_analyst:rate_limit:1"])
    with pytest.raises(InjectedProviderFault) as excinfo:
        injector.check("tech_analyst", "m")
    detail = excinfo.value.record["detail"]
    assert "rehearsal fault injection" in detail
    assert "not a real provider failure" in detail
