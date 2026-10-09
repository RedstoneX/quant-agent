"""`MacroConfig`, lifted verbatim out of `src/config/__init__.py` (the file-size
ratchet). Public import path is unchanged: `from src.config import MacroConfig`.
"""

from pydantic import BaseModel, Field, model_validator


class MacroConfig(BaseModel):
    """FRED fetch resilience for the macro seat (src/data/macro.py).

    Added Phase 4.2 after production evidence of a full outage: on
    2026-08-26 17:01:29-17:03:49 UTC all nine FRED series failed in ONE run
    with "The read operation timed out", using what was then a
    single-retry / flat-2-second-backoff policy hardcoded as module
    constants (`_FRED_MAX_RETRIES` / `_FRED_RETRY_BACKOFF_S` /
    `_FRED_BREAKER_AFTER_FAILED_SERIES`, added 2026-08-20 off an earlier,
    smaller incident). Per the repo's standing rule that a number able to
    change behaviour is an operator setting, not a constant buried in code,
    these move here — mirrored through to `MacroDataProvider.__init__` the
    same way `smart_money.insider_*` threads into `SECForm4Provider`
    (src/pipeline.py passes every field below explicitly at construction).
    """

    request_timeout_s: float = Field(default=15.0, ge=1.0, le=60.0)
    """Per-HTTP-request socket timeout. 15s is generous — FRED typically
    responds in well under a second; slower than that is network/service
    trouble worth degrading gracefully from rather than hanging on."""

    max_retries: int = Field(default=2, ge=0, le=5)
    """Bounded retries per series BEFORE the consecutive-failure breaker
    (below) trips. Raised from the old hardcoded 1 — a single retry with a
    flat 2s backoff was not enough margin to ride out the network blips
    behind the 2026-08-26 incident. Still bounded: see
    breaker_after_failed_series and total_fetch_deadline_s for why more
    retries can't turn into an unbounded stall."""

    retry_backoff_base_s: float = Field(default=2.0, gt=0, le=30.0)
    """First retry's backoff, in seconds. Doubles each subsequent retry,
    capped at retry_backoff_max_s (see MacroDataProvider._next_backoff)."""

    retry_backoff_max_s: float = Field(default=8.0, gt=0, le=60.0)
    """Ceiling on the exponential backoff — keeps a multi-retry series from
    ballooning its own wait time."""

    retry_backoff_jitter_s: float = Field(default=1.0, ge=0, le=10.0)
    """Uniform random jitter, 0..this many seconds, added to every backoff
    sleep — so a genuine outage spanning many series doesn't retry all of
    them in lockstep against FRED."""

    breaker_after_failed_series: int = Field(default=1, ge=1, le=9)
    """After this many series have each exhausted their own retries and
    still failed, the breaker trips: every subsequent series in the SAME
    get_macro_summary() call gets a single attempt (no retries), because a
    run that has already lost this many series in a row reads as a genuine
    outage, not a flake — full retries on every remaining series would
    only multiply the stall. A success anywhere resets the counter.
    Default 1 (tighter than the old hardcoded 2) because there are now up
    to fifteen series to get through inside the same shared
    total_fetch_deadline_s budget, not nine."""

    total_fetch_deadline_s: float = Field(default=90.0, ge=10.0, le=300.0)
    """Hard wall-clock ceiling for one get_macro_summary() call, independent
    of the retry/backoff arithmetic above. `MacroDataProvider` clips every
    request's timeout AND every retry's backoff sleep to whatever remains
    of this budget, and skips any series not yet started once it's
    exhausted — so this is a real ceiling on added wall-clock, not merely
    an upper bound implied by retry-count × timeout arithmetic. This is
    what keeps a full FRED outage from stalling the live trading session
    that reads this feed."""

    @model_validator(mode="after")
    def _resilience_bounds_are_well_formed(self):
        if self.retry_backoff_base_s > self.retry_backoff_max_s:
            raise ValueError(
                "macro.retry_backoff_base_s must be <= retry_backoff_max_s; "
                f"got {self.retry_backoff_base_s} > {self.retry_backoff_max_s}"
            )
        if self.total_fetch_deadline_s < self.request_timeout_s:
            raise ValueError(
                "macro.total_fetch_deadline_s must be >= request_timeout_s "
                "— a deadline shorter than one request's own timeout would "
                f"abort every fetch immediately without ever really trying; "
                f"got {self.total_fetch_deadline_s} < {self.request_timeout_s}"
            )
        return self
