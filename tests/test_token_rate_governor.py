"""The desk must never flood a provider — owner instruction, 2026-08-31.

Every one of the eleven rate limits in three weeks of logs hit the same agent,
the Technical Analyst, and no other agent was declined once. It is the only
one that bursts: ~314,000 tokens in 80 seconds, about 252,000 per minute,
while everything else sends 20-30k in a single call.

Nothing bounded that. Concurrency was capped at three requests and per-call
context was capped, but a rate limit counts TOKENS PER MINUTE — and three
concurrent 80,000-token requests satisfy a cap of three while being exactly
what gets refused. The desk left the rate limiting to the provider, then
treated being told no as an emergency.
"""

from __future__ import annotations

import threading

import pytest

from src.token_rate import WINDOW_S, TokenRateGovernor


class _Clock:
    """Virtual time: these tests must not actually sleep."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def _governor(limit=1000, max_wait_s=120.0):
    clock = _Clock()
    return TokenRateGovernor(
        "test", limit, max_wait_s=max_wait_s, sleep=clock.sleep, clock=clock
    ), clock


# ------------------------------------------------------------- the ceiling


def test_requests_under_the_ceiling_are_not_delayed():
    gov, clock = _governor(limit=1000)
    assert gov.charge(400) == 0.0
    assert gov.charge(400) == 0.0
    assert clock.slept == 0.0


def test_a_request_that_would_breach_the_ceiling_waits():
    gov, clock = _governor(limit=1000)
    gov.charge(900)
    waited = gov.charge(200)
    assert waited > 0, "the burst must be paced, not sent"
    assert gov.waits == 1


def test_the_wait_is_only_as_long_as_the_window_needs():
    """Pacing must not overshoot: once the oldest tokens age out of the
    minute, the request goes. A governor that waited longer than necessary
    would cost the desk time at the open for nothing."""
    gov, clock = _governor(limit=1000)
    gov.charge(1000)
    waited = gov.charge(500)
    assert waited == pytest.approx(WINDOW_S, abs=1.0)


def test_tokens_age_out_of_the_window():
    gov, clock = _governor(limit=1000)
    gov.charge(1000)
    clock.now += WINDOW_S + 1
    assert gov.charge(1000) == 0.0


# ------------------------------------------- bounded, never a second outage


def test_the_wait_is_bounded_and_the_breach_is_loud(caplog):
    """A trading session has a hard outer kill. A governor that could stall
    forever would trade one outage for another, so the wait is capped — and
    when the cap is hit the request goes anyway, loudly. Silence would mean
    nobody ever learns the ceiling is set wrong."""
    gov, clock = _governor(limit=1000, max_wait_s=5.0)
    gov.charge(1000)
    with caplog.at_level("CRITICAL"):
        waited = gov.charge(1000)
    assert waited <= 5.0
    assert gov.breaches == 1
    assert "EXCEEDED its ceiling" in caplog.text


def test_a_single_request_larger_than_the_whole_budget_is_not_stalled():
    """No amount of waiting makes it fit, so waiting is pure delay. The
    ceiling is a rate limit, not a size limit — stranding the session with no
    analysis is the worse outcome."""
    gov, clock = _governor(limit=1000)
    assert gov.charge(5000) == 0.0
    assert clock.slept == 0.0


# ------------------------------------------------ the window tells the truth


def test_the_estimate_is_reconciled_to_the_providers_real_count():
    """The window must reflect what was really sent. Without this a
    systematically wrong estimator would quietly make the ceiling mean
    something other than it says."""
    gov, _ = _governor(limit=1000)
    gov.charge(300)
    gov.reconcile(300, 900)
    assert gov.snapshot()["tokens_in_window"] == 900


def test_reconciling_downward_returns_headroom():
    gov, _ = _governor(limit=1000)
    gov.charge(900)
    gov.reconcile(900, 100)
    assert gov.snapshot()["tokens_in_window"] == 100
    assert gov.charge(800) == 0.0


def test_reconciliation_never_drives_the_window_negative():
    gov, _ = _governor(limit=1000)
    gov.charge(100)
    gov.reconcile(100, -500)
    assert gov.snapshot()["tokens_in_window"] >= 0


# ----------------------------------------------------------------- threading


def test_concurrent_callers_cannot_oversend():
    """Specialists run in parallel. Two threads that both read the window
    before either writes would let a burst straight through — which is the
    exact failure this governor exists to stop."""
    gov, _ = _governor(limit=1000, max_wait_s=0.0)
    barrier = threading.Barrier(4)

    def send():
        barrier.wait()
        gov.charge(400)

    threads = [threading.Thread(target=send) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 x 400 = 1600 against a 1000 ceiling. With max_wait_s=0 they all
    # proceed, but every one must be COUNTED — an uncounted send is an
    # invisible flood.
    assert gov.snapshot()["tokens_in_window"] == 1600


def test_a_positive_limit_is_required():
    with pytest.raises(ValueError):
        TokenRateGovernor("bad", 0)


# ------------------------------------------------------- wired into the desk


def test_every_provider_domain_has_a_governor():
    """An ungoverned domain is an unbounded one. If a new provider is added,
    this fails until it is given a ceiling."""
    from src.agents.base import _TOKEN_GOVERNORS

    assert set(_TOKEN_GOVERNORS) == {
        "openrouter", "openai", "anthropic", "deepseek", "google",
    }
    for governor in _TOKEN_GOVERNORS.values():
        assert governor.tokens_per_minute > 0


def test_the_ceiling_is_below_the_burst_that_was_being_refused():
    """150k/min is not arbitrary: the measured burst that OpenRouter declined
    was ~252k/min. A ceiling above that would govern nothing."""
    from src.agents.base import _TOKEN_GOVERNORS

    assert _TOKEN_GOVERNORS["openrouter"].tokens_per_minute < 252_000


def test_the_google_ceiling_is_80pct_of_the_measured_free_tier():
    """200,000 TPM is 80% of the 250,000 TPM ceiling read off the owner's own
    AI Studio dashboard (2026-08-31) — a measured margin, not a guessed one,
    which was the owner's explicit objection to an earlier hand-picked
    ceiling."""
    from src.agents.base import _TOKEN_GOVERNORS

    assert _TOKEN_GOVERNORS["google"].tokens_per_minute == pytest.approx(200_000)
    assert _TOKEN_GOVERNORS["google"].tokens_per_minute == pytest.approx(250_000 * 0.8)


def test_a_failover_is_charged_to_the_configured_fallback_providers_governor():
    """Keyed on which PATH the attempt is taking (is_failover), not on
    sniffing the model string: a failover spends the FALLBACK provider's
    rate limit, and charging it to the primary's governor would let a
    failover storm past the ceiling. The fallback (provider, model) pair is
    now configurable, so a primary success must charge the PRIMARY's
    governor and a failover attempt must charge the FALLBACK's governor,
    regardless of what either model string looks like."""
    from src.agents.base import _governor_domain_for

    openrouter_agent = type(
        "A", (), {"_provider": "openrouter", "_fallback_provider": "google"},
    )()
    # Primary success (is_failover=False, the default): charged to the
    # agent's own configured provider.
    assert _governor_domain_for("google/gemini-3.5-flash-lite", openrouter_agent) == "openrouter"
    # A failover attempt: charged to the FALLBACK provider, even though nothing
    # about the model string ("claude-opus-4-7", the old hardcoded fallback)
    # says so any more.
    assert _governor_domain_for(
        "claude-opus-4-7", openrouter_agent, is_failover=True,
    ) == "google"


def test_governor_domain_falls_back_safely_for_an_unrecognized_provider():
    """A malformed/missing agent attribute must not KeyError the governor
    lookup — it degrades to a known-safe default rather than crashing the
    request that was about to be paced."""
    from src.agents.base import _governor_domain_for

    weird_agent = type("A", (), {"_provider": "not-a-real-provider"})()
    assert _governor_domain_for("x", weird_agent) == "openai"
    no_fallback_agent = type("A", (), {"_provider": "openai"})()
    assert _governor_domain_for("x", no_fallback_agent, is_failover=True) == "openrouter"
