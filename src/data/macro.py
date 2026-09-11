import logging
import random
import socket
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from fredapi import Fred

from src.trading_calendar import et_today

logger = logging.getLogger(__name__)

# --- FRED fetch resilience -------------------------------------------------
#
# History: this used to be four module constants (_FRED_TIMEOUT_S,
# _FRED_MAX_RETRIES, _FRED_RETRY_BACKOFF_S, _FRED_BREAKER_AFTER_FAILED_SERIES,
# added 2026-08-20 off a soak test showing 14 timeouts spread across
# DIFFERENT series on different runs — the same series succeeded 30
# minutes earlier/later, so a single short-backoff retry recovered the
# observed transient mode). Production evidence since then showed that
# policy was not enough: on 2026-08-26 17:01:29-17:03:49 UTC ALL NINE
# series failed in one run with "The read operation timed out" — the
# single retry / flat 2s backoff never gave a real chance to ride out
# more than a few seconds of trouble, and the macro analyst ran the whole
# session on all-None inputs with nothing operator-facing to show it.
#
# Per the repo's standing rule that a number able to change behaviour is
# an operator setting, not a constant buried in code, these are now
# constructor parameters (mirroring how `smart_money.insider_*` and
# `SECForm4Provider.refresh_deadline_s` are threaded through
# src/data/smart_money.py / src/config.py) — see
# `src/config.py::MacroConfig` and the `macro:` block in
# `config/settings.yaml` for the operator-facing defaults and reasoning
# behind each one. The values baked in below as constructor defaults
# exist only so a caller that builds a `MacroDataProvider` directly
# (tests, `ops/commissioning/verify_commissioning.py`) without wiring a
# config still gets sane, bounded behavior.
#
# The one hard guarantee that must survive any future retuning of these
# numbers: `total_fetch_deadline_s` is a real wall-clock ceiling for one
# `get_macro_summary()` call, enforced by clipping every request's timeout
# and every retry's backoff sleep to whatever budget remains — not merely
# an upper bound implied by retry-count × timeout arithmetic. That is what
# keeps a full FRED outage from stalling the live trading session that
# reads this feed, regardless of how max_retries/backoff get retuned later.


# Exception text longer than this is truncated before it reaches a log line
# or the macro analyst's prompt — mirrors src.data.news._FAILURE_REASON_MAX_LEN
# (same shape of problem: feed/series failures are almost always short, but a
# verbose exception should never blow up the coverage section of the prompt).
_FAILURE_REASON_MAX_LEN = 200


@dataclass
class SeriesFailure:
    """One configured FRED series that did not return usable data on a
    get_macro_summary() call. Mirrors src.data.news.FeedFailure."""

    series_id: str
    reason: str


#: The four freshness states one FRED series can be in on a run. This is
#: deliberately NOT a day count — see `SeriesFreshness` for why a calendar
#: threshold is the wrong test for macro data.
FRESHNESS_CURRENT = "current"
FRESHNESS_OVERDUE = "overdue"
FRESHNESS_UNKNOWN = "unknown"
FRESHNESS_EMPTY = "empty"


@dataclass
class SeriesFreshness:
    """Whether the value we hold for one FRED series is the latest reading
    that EXISTS, and — separately — whether a newer reading should have
    arrived by now and hasn't.

    Why this replaced a day count (2026-09-11)
    ------------------------------------------
    Every freshness test on macro data used to be a calendar-day
    threshold: `staleness_days <= 1` for the regime-shift gate, `> 3`
    (daily) / `> 55` (monthly) for the confidence gate. Measured
    consequence: the regime-shift gate fired on 14 of 27 retained
    production runs (52%, 2026-08-17..09-02) because FRED's real
    publication lag on its DAILY series is about 2 business days — six
    independent production checkpoints show treasury and fed_funds_rate at
    `staleness_days=2` in 6/6 samples, never 1. The gate demanded a print
    that does not exist at the time of asking.

    A day count is the wrong test in principle, not just mis-tuned. Macro
    series are not published daily: CPI and the employment report are
    monthly, FOMC decisions land roughly every six weeks, GDP is quarterly.
    A CPI reading 20 days old is not stale — it IS the current reading,
    because no newer one exists. Rejecting it for age throws away the only
    number there is. A macro reading goes stale when a NEWER print
    supersedes it (or should have and didn't), never when a calendar
    threshold passes. Same correction the owner directed for smart-money
    evidence, which shipped as PR #288 (age-based decay removed; structural
    validity and corroboration kept).

    What establishes each state, from real data only
    -----------------------------------------------
    * `current` — we hold FRED's latest published observation for the
      series and no newer one is due yet. Use it however old it is.
    * `overdue` — a newer print should have been published by now and we
      do not have it: a publication failure, a fetch failure, a government
      shutdown. A REAL problem, surfaced (see `MacroCoverage.overdue`,
      `data_status["macro"] == "release_overdue"`).
    * `unknown` — freshness could not be established because FRED's series
      metadata did not come back this run. Honest third state: it does not
      claim fresh and does not claim broken.
    * `empty` — FRED returned no usable observation at all. Never passed
      off downstream as a real reading.

    The two real signals used, and the one approximation
    ----------------------------------------------------
    1. "Is this the latest available reading?" — answered structurally, not
       by age. `_safe_get_series` queries FRED with an `observation_start`
       and NO `observation_end`, so FRED returns everything it has
       published; the last row IS its latest published observation by
       construction. The metadata field `observation_end` is checked
       against it as a cross-check, so a fetch that silently returned less
       than FRED holds reads as `overdue`, not as fine.
    2. "When is the next print due?" — derived per series, never from a
       hardcoded per-series calendar table:
         * cadence = the LONGEST gap that series itself shows between its
           own consecutive observations in the window already fetched. Real
           data, so weekends, market holidays, and 28-vs-31-day months are
           covered by the series' own behaviour rather than by a constant.
         * publication lag = FRED's own metadata pair, `last_updated`
           minus `observation_end` — how far behind its own reference date
           this series' current print actually published.
       `expected_next_by = last_valued_observation + cadence + lag`. Past
       that date with no newer print, a print is genuinely missing.

    The approximation, stated rather than hidden: `last_updated` is the
    last time FRED touched the series, which is normally the publication of
    the current print but can also be a revision of older data, so the
    derived lag can be shorter than the true release lag. Consequence is
    bounded and one-directional — `expected_next_by` can land slightly
    early, i.e. a genuinely-late release can be flagged `overdue` a day or
    two before the agency's scheduled date. That direction is the safe one
    (it over-reports a real problem rather than under-reporting it) and it
    flags, never discards. The exact per-series release schedule is
    knowable (FRED's `/fred/series/release` + `/fred/release/dates`, and
    `src/data/event_calendar.py` already fetches release dates for seven
    releases) but it would cost two extra API calls per series inside a
    fetch path with a hard wall-clock ceiling; `get_series_vintage_dates`
    would give real publication history but is unbounded in size (every
    vintage ever, thousands of rows for a daily series). Both were
    considered and rejected on cost, not because the day count was
    defensible.
    """

    series_id: str
    status: str
    latest_observation: date | None = None
    expected_next_by: date | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        """True when the held reading is the latest that exists and nothing
        newer is due — `unknown` counts as usable because the reading is
        still FRED's latest published observation by construction of the
        query (point 1 above); only the DUE-date half is unverifiable
        without metadata, and refusing to use data on unverifiable metadata
        is exactly how the old gate became unreachable."""
        return self.status in (FRESHNESS_CURRENT, FRESHNESS_UNKNOWN)

    def describe(self) -> str:
        if self.status == FRESHNESS_OVERDUE:
            return (
                f"{self.series_id}: OVERDUE — {self.detail}"
                if self.detail else f"{self.series_id}: OVERDUE"
            )
        if self.status == FRESHNESS_EMPTY:
            return f"{self.series_id}: no observation returned"
        if self.status == FRESHNESS_UNKNOWN:
            return (
                f"{self.series_id}: freshness UNVERIFIED ({self.detail})"
                if self.detail else f"{self.series_id}: freshness UNVERIFIED"
            )
        return f"{self.series_id}: latest published reading"


@dataclass
class MacroCoverage:
    """How much of the configured FRED series set actually came back on one
    get_macro_summary() call — the macro-side counterpart to
    src.data.news.NewsCoverage (2026-08-28 news fix), built for the exact
    same reason: before this, a fully-failed FRED fetch (the 2026-08-26
    17:01-17:03 UTC incident — all nine series timed out in one run)
    produced nothing but WARNING log lines. The macro analyst ran on
    all-None inputs and — correctly, per its own inputs — called critical
    missing data and downgraded confidence, but the desk's operator-facing
    surface never showed anything was wrong; a log line alone was exactly
    the failure mode being fixed.

    This object is the single source of truth for that fact from here on.
    It is threaded into the macro analyst's own prompt (build_user_message,
    src/agents/macro_analyst.py) AND into the deterministic data_status the
    operator surface reads (src/pipeline_stages.py) — which is what
    trader_feed.py / notifier.py already render as the "⚠️ Data degraded"
    banner (that banner fires on ANY data_status value other than ok/empty,
    so it needed no changes of its own to pick this up).
    """

    configured: int
    succeeded: int
    failed: list[SeriesFailure]
    #: Series that DID return data, but whose next print is overdue (see
    #: `SeriesFreshness`). A separate axis from `failed` on purpose: the
    #: fetch worked, the publication did not. Keyword-defaulted so every
    #: existing construction site and test double keeps working.
    overdue: list[SeriesFreshness] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def overdue_count(self) -> int:
        return len(self.overdue)

    @property
    def complete(self) -> bool:
        """True only when every configured series returned successfully.

        Zero configured series is deliberately NOT complete — that is a
        configuration error, not full coverage of nothing (mirrors
        NewsCoverage.complete).
        """
        return self.configured > 0 and self.failed_count == 0

    @property
    def status(self) -> str:
        """One word for data_status[...] / logs — mirrors the ok / partial /
        failed vocabulary MorningResearchStage already uses for `news`/`tech`
        (src/pipeline_stages.py), so this reuses an existing convention
        rather than inventing a parallel one.
        """
        if self.configured == 0 or self.succeeded == 0:
            return "failed"
        if self.failed:
            return "partial"
        return "ok"

    def describe(self) -> str:
        """Human-readable one-liner for the macro analyst's prompt and log
        lines. Deliberately names what happened rather than going quiet —
        a reader (human or model) must not mistake missing input for "the
        indicator is calm right now"."""
        if self.configured == 0:
            return "Macro coverage: NO FRED series configured (misconfiguration)."
        overdue_text = ""
        if self.overdue:
            overdue_text = (
                " OVERDUE PRINTS: "
                + "; ".join(f.describe() for f in self.overdue)
                + ". These series returned their latest published reading, but "
                "a NEWER print is past due by the series' own cadence and "
                "publication lag — a publication or fetch failure, not normal "
                "release timing. Do not read the held value as current."
            )
        if not self.failed:
            return (
                f"Macro coverage: {self.succeeded}/{self.configured} FRED "
                f"series returned data. Full coverage." + overdue_text
            )
        names = ", ".join(f"{f.series_id} ({f.reason})" for f in self.failed)
        return (
            f"Macro coverage: {self.succeeded}/{self.configured} FRED series "
            f"returned data this run. FAILED: {names}. Treat this as a "
            f"coverage GAP, not a confirmed reading — a missing indicator is "
            f"not evidence that indicator is calm." + overdue_text
        )


def _et_lookback_start(days: int) -> pd.Timestamp:
    """Pandas timestamp `days` days before the current ET trading day,
    used as the `observation_start` for FRED queries.

    Previously these sites used ``pd.Timestamp.now() - pd.Timedelta(days=N)``
    which is host-TZ-naive: a Linux-UTC host and a Mac-ET host would
    compute different lookback boundaries for the same calendar day.
    FRED has daily resolution so the practical drift is at most one
    daily observation — but the CLAUDE.md invariant is "any host TZ
    must produce the same data", and the staleness_days computation
    in this module already uses et_today() for the upper bound. Anchoring
    the lookback to et_today() too keeps the window symmetric.
    """
    return pd.Timestamp(et_today()) - pd.Timedelta(days=days)


class MacroDataProvider:
    def __init__(
        self,
        api_key: str,
        *,
        request_timeout_s: float = 15.0,
        max_retries: int = 2,
        retry_backoff_base_s: float = 2.0,
        retry_backoff_max_s: float = 8.0,
        retry_backoff_jitter_s: float = 1.0,
        breaker_after_failed_series: int = 1,
        total_fetch_deadline_s: float = 90.0,
    ):
        # Fail fast on missing/empty FRED_API_KEY. Without this guard, an
        # unset key silently fails on every series fetch inside
        # macro_analyst's run, leaving macro_summary as all-None — and
        # the symptom (PM sees `regime: unknown`, downgrades exposure) is
        # hours away from the root cause (wrong .env). Better to crash
        # at construction so the operator notices immediately at startup.
        if not api_key or not api_key.strip():
            raise ValueError(
                "FRED_API_KEY is empty or unset. Set it in .env — macro "
                "analysis cannot proceed without FRED access. Pass an "
                "explicit non-empty string here only if you intend to "
                "exercise the offline / mock path."
            )
        self.fred = Fred(api_key=api_key)
        # Defensive clamping mirrors SECForm4Provider's constructor
        # (src/data/smart_money.py) — a caller/config typo can't produce a
        # zero/negative timeout or an inverted backoff window.
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base_s = max(0.0, float(retry_backoff_base_s))
        self.retry_backoff_max_s = max(self.retry_backoff_base_s, float(retry_backoff_max_s))
        self.retry_backoff_jitter_s = max(0.0, float(retry_backoff_jitter_s))
        self.breaker_after_failed_series = max(1, int(breaker_after_failed_series))
        # Never below one request's own timeout — a shorter deadline would
        # abort every fetch immediately (see MacroConfig's validator, which
        # enforces the same invariant on the config side).
        self.total_fetch_deadline_s = max(self.request_timeout_s, float(total_fetch_deadline_s))
        # Consecutive fully-failed series this provider instance. Once it
        # reaches breaker_after_failed_series the retry layer stands down
        # (single attempt per series) — a genuine outage should degrade
        # fast, not multiply its own latency. Persists across
        # get_macro_summary() calls (existing behavior, unchanged) — a
        # success anywhere resets it.
        self._consecutive_failed_series = 0
        # Wall-clock deadline for the CURRENT get_macro_summary() call, in
        # time.monotonic() units. None outside of that call (e.g. a direct
        # get_vix() call has no shared-budget concept to enforce).
        self._deadline: float | None = None
        # Per-get_macro_summary()-call coverage accounting, reset at the
        # top of that method and consumed into `self.last_coverage` at the
        # end of it.
        self._run_configured = 0
        self._run_succeeded = 0
        self._run_failed: list[SeriesFailure] = []
        # Per-series freshness for the CURRENT call, keyed by series id (see
        # SeriesFreshness). Read back by the fetchers below so each
        # indicator dict carries its own freshness state, and snapshotted
        # into `last_coverage.overdue` at the end of get_macro_summary().
        self._run_freshness: dict[str, SeriesFreshness] = {}
        # FRED series metadata (`/fred/series`) for this call only. A series
        # is fetched once per get_macro_summary(), so this exists for the
        # benefit of direct single-indicator calls, and is cleared per call
        # because the metadata changes the moment a new print lands.
        self._series_info_cache: dict[str, dict[str, str] | None] = {}
        # Coverage snapshot from the most recent get_macro_summary() call —
        # the side channel pipeline_stages.py reads to set
        # data_status["macro"], deliberately NOT folded into
        # get_macro_summary()'s own return value: that dict is consumed as
        # a bare `dict` from three separate call sites across
        # pipeline_stages.py / pipeline.py (position review, evening
        # analysis) and by risk_manager / position_reviewer /
        # evening_analyst downstream, so changing its shape to a tuple
        # would be a much larger blast radius than this fix calls for.
        self.last_coverage: MacroCoverage | None = None

    def _next_backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, clipped to whatever remains of
        the fetch deadline so a retry sleep can never itself blow the
        wall-clock ceiling get_macro_summary() promises callers.

        attempt is 0-indexed (the attempt that just failed). Backoff
        doubles each attempt from retry_backoff_base_s, capped at
        retry_backoff_max_s, then gets uniform(0, retry_backoff_jitter_s)
        added — jitter keeps a many-series outage from retrying every
        series in lockstep against FRED.
        """
        base = min(
            self.retry_backoff_base_s * (2 ** attempt),
            self.retry_backoff_max_s,
        )
        backoff = base + random.uniform(0, self.retry_backoff_jitter_s)
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            backoff = max(0.0, min(backoff, remaining))
        return backoff

    def _note_coverage(self, series_id: str, *, ok: bool, reason: str) -> None:
        self._run_configured += 1
        if ok:
            self._run_succeeded += 1
        else:
            self._run_failed.append(SeriesFailure(
                series_id=series_id,
                reason=(reason or "unknown")[:_FAILURE_REASON_MAX_LEN],
            ))

    # --- freshness: latest-available, not calendar age ---------------------

    @staticmethod
    def _as_date(value) -> date | None:
        """Parse one FRED date/timestamp field into a date, or None.

        Deliberately strict and total: FRED metadata arrives as strings
        (`observation_end` = "2026-09-09", `last_updated` =
        "2026-09-10 07:31:05-05"), and a test double or a redesigned
        response can hand back anything at all. Anything unparseable
        becomes None, which downstream reads as "freshness unverified" —
        never as fresh.
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = pd.Timestamp(value)
        except Exception:
            return None
        if parsed is None or pd.isna(parsed):
            return None
        try:
            return parsed.date()
        except Exception:
            return None

    def _series_info(self, series_id: str) -> dict[str, str] | None:
        """FRED's own metadata for one series (`/fred/series`), or None.

        One attempt, no retries: this is the DUE-date input, not the data
        itself, and the wall-clock ceiling `get_macro_summary()` promises
        its caller belongs to the observations. A miss degrades to
        `freshness: unknown`, which is honest and costs nothing downstream.

        Only the two fields the due-date derivation needs are kept, and
        only when both parse as real dates — so a MagicMock or a
        redesigned response cannot masquerade as metadata.
        """
        if series_id in self._series_info_cache:
            return self._series_info_cache[series_id]
        info: dict[str, str] | None = None
        if self._deadline is not None and time.monotonic() >= self._deadline:
            logger.debug(
                "Skipping FRED metadata for %s — fetch deadline already "
                "exceeded; freshness will report unknown", series_id,
            )
            self._series_info_cache[series_id] = None
            return None
        prev_timeout = socket.getdefaulttimeout()
        try:
            remaining = (
                self._deadline - time.monotonic() if self._deadline is not None
                else self.request_timeout_s
            )
            socket.setdefaulttimeout(min(self.request_timeout_s, max(1.0, remaining)))
            raw = self.fred.get_series_info(series_id)
            observation_end = None
            last_updated = None
            # pandas Series (fredapi's real return) and plain mappings both
            # support .get(); anything else is not metadata.
            getter = getattr(raw, "get", None)
            if callable(getter):
                observation_end = self._as_date(getter("observation_end"))
                last_updated = self._as_date(getter("last_updated"))
            if observation_end is not None and last_updated is not None:
                info = {
                    "observation_end": observation_end.isoformat(),
                    "last_updated": last_updated.isoformat(),
                }
        except Exception as e:  # noqa: BLE001 — any shape degrades to unknown
            logger.warning(
                "FRED metadata unavailable for %s: %s — freshness for this "
                "series will report unknown", series_id, e,
            )
        finally:
            socket.setdefaulttimeout(prev_timeout)
        self._series_info_cache[series_id] = info
        return info

    def _record_freshness(self, series_id: str, raw: pd.Series) -> SeriesFreshness:
        """Classify one series as current / overdue / unknown / empty.

        `raw` is the series exactly as FRED returned it — BEFORE `.dropna()`
        — because the NaN rows are themselves information: FRED emits a row
        with value "." for a market holiday on a daily series, so including
        them is what lets `observation_end` be compared honestly, and
        excluding them is what identifies the last reading that has an
        actual value.

        See `SeriesFreshness` for the full reasoning and for the one stated
        approximation in the due-date derivation.
        """
        def _store(freshness: SeriesFreshness) -> SeriesFreshness:
            self._run_freshness[series_id] = freshness
            return freshness

        valued = raw.dropna() if raw is not None and len(raw) else raw
        if valued is None or len(valued) == 0:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_EMPTY,
                detail="FRED returned no usable observation",
            ))

        try:
            last_obs = pd.Timestamp(valued.index[-1]).date()
        except Exception:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_UNKNOWN,
                detail="observation dates unreadable",
            ))

        info = self._series_info(series_id)
        if info is None:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_UNKNOWN,
                latest_observation=last_obs,
                detail="FRED series metadata did not come back this run",
            ))
        observation_end = date.fromisoformat(info["observation_end"])
        last_updated = date.fromisoformat(info["last_updated"])

        # Cross-check 1: FRED says it has published further than what we
        # received. The query sets no observation_end, so this should be
        # impossible — if it happens, our data is NOT the latest available
        # and that is a real problem, not normal release timing. (Trailing
        # NaN rows are not this case: they are in `raw` and are counted
        # below via the cadence, because a holiday gap is normal and a long
        # run of no-value rows is not.)
        try:
            last_row = pd.Timestamp(raw.index[-1]).date()
        except Exception:
            last_row = last_obs
        if observation_end > last_row:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_OVERDUE,
                latest_observation=last_obs,
                detail=(
                    f"FRED has published observations through "
                    f"{observation_end.isoformat()} but this fetch returned "
                    f"nothing after {last_row.isoformat()}"
                ),
            ))

        # Cadence, from the series' own observed behaviour: the longest gap
        # it showed between consecutive real readings in the window already
        # fetched. No per-series calendar table, and weekends, market
        # holidays and uneven month lengths are all covered by the data
        # itself. Fewer than two readings means the window cannot show a
        # cadence at all — unknown, not assumed.
        try:
            index = pd.DatetimeIndex(valued.index)
            gaps = index.to_series().diff().dropna()
            cadence_days = int(max(1, gaps.dt.days.max())) if len(gaps) else 0
        except Exception:
            cadence_days = 0
        if cadence_days <= 0:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_UNKNOWN,
                latest_observation=last_obs,
                detail=(
                    "fewer than two readings in the fetched window — this "
                    "series' own publication cadence cannot be derived from it"
                ),
            ))

        # Publication lag, from FRED's own metadata pair: how far behind its
        # reference date this series' current print actually published.
        lag_days = max(0, (last_updated - observation_end).days)
        expected_next_by = last_obs + timedelta(days=cadence_days + lag_days)
        today = et_today()
        if today > expected_next_by:
            return _store(SeriesFreshness(
                series_id=series_id,
                status=FRESHNESS_OVERDUE,
                latest_observation=last_obs,
                expected_next_by=expected_next_by,
                detail=(
                    f"latest reading is {last_obs.isoformat()}; on this "
                    f"series' own cadence ({cadence_days}d between readings) "
                    f"and its own publication lag ({lag_days}d) a newer print "
                    f"was due by {expected_next_by.isoformat()}"
                ),
            ))
        return _store(SeriesFreshness(
            series_id=series_id,
            status=FRESHNESS_CURRENT,
            latest_observation=last_obs,
            expected_next_by=expected_next_by,
            detail=(
                f"latest published reading ({last_obs.isoformat()}); next due "
                f"by {expected_next_by.isoformat()}"
            ),
        ))

    def _freshness_fields(self, *series_ids: str) -> dict:
        """The freshness half of one indicator's payload.

        Several indicators are built from more than one series (the yield
        curve from DGS3MO/DGS2/DGS10, inflation from CPI/core/PCE). The
        indicator is only as fresh as its WORST constituent: an overdue
        leg anywhere means the indicator cannot be read as current, and an
        unverified leg means it cannot be asserted current either. Ordered
        worst-first below.
        """
        # getattr, not attribute access: a couple of existing tests build a
        # provider with `__new__` and stub `_safe_get_series`, so the
        # per-call state this reads may never have been initialised. An
        # indicator whose freshness was never recorded reads as unverified,
        # which is the honest answer in that case too.
        recorded = getattr(self, "_run_freshness", None) or {}
        found = [recorded[sid] for sid in series_ids if sid in recorded]
        if not found:
            return {"freshness": FRESHNESS_UNKNOWN, "freshness_detail": ""}
        for wanted in (
            FRESHNESS_OVERDUE, FRESHNESS_EMPTY, FRESHNESS_UNKNOWN, FRESHNESS_CURRENT,
        ):
            for f in found:
                if f.status == wanted:
                    return {"freshness": f.status, "freshness_detail": f.describe()}
        return {"freshness": FRESHNESS_UNKNOWN, "freshness_detail": ""}

    def _safe_get_series(self, series_id: str, **kwargs) -> pd.Series:
        # Hard wall-clock ceiling check FIRST: if get_macro_summary()'s
        # total_fetch_deadline_s has already elapsed (e.g. earlier series in
        # this same run ate the whole budget retrying), skip this series
        # without even attempting it. This is what actually bounds the
        # worst case — the retry/backoff math below is a best-effort
        # recovery mechanism, not a hard ceiling by itself.
        if self._deadline is not None and time.monotonic() >= self._deadline:
            logger.warning(
                "FRED fetch deadline (%.0fs) already exceeded — skipping %s "
                "without an attempt", self.total_fetch_deadline_s, series_id,
            )
            self._consecutive_failed_series += 1
            self._note_coverage(series_id, ok=False, reason="fetch_deadline_exceeded")
            self._run_freshness[series_id] = SeriesFreshness(
                series_id=series_id, status=FRESHNESS_EMPTY,
                detail="not fetched — fetch deadline exceeded",
            )
            return pd.Series(dtype=float)

        retries = (
            self.max_retries
            if self._consecutive_failed_series < self.breaker_after_failed_series
            else 0
        )
        prev_timeout = socket.getdefaulttimeout()
        result = None
        transport_failed = False
        failure_reason = ""
        try:
            for attempt in range(retries + 1):
                remaining = (
                    self._deadline - time.monotonic() if self._deadline is not None
                    else self.request_timeout_s
                )
                if remaining <= 0:
                    transport_failed = True
                    failure_reason = "fetch_deadline_exceeded"
                    logger.warning(
                        "FRED fetch deadline exceeded before attempt %d/%d "
                        "for %s — degrading now",
                        attempt + 1, retries + 1, series_id,
                    )
                    break
                # Scoped socket timeout so other modules' sockets aren't
                # affected, clipped to whatever's left of the deadline so
                # the LAST in-flight request can't itself blow the budget.
                socket.setdefaulttimeout(min(self.request_timeout_s, remaining))
                try:
                    result = self.fred.get_series(series_id, **kwargs)
                    break
                except Exception as e:
                    failure_reason = str(e) or type(e).__name__
                    if attempt < retries:
                        backoff = self._next_backoff(attempt)
                        logger.warning(
                            "FRED API error for %s (attempt %d/%d): %s — "
                            "retrying in %.1fs",
                            series_id, attempt + 1, retries + 1, e, backoff,
                        )
                        if backoff > 0:
                            time.sleep(backoff)
                        continue
                    logger.warning("FRED API error for %s: %s", series_id, e)
                    transport_failed = True
        finally:
            socket.setdefaulttimeout(prev_timeout)

        if transport_failed:
            self._consecutive_failed_series += 1
            self._note_coverage(series_id, ok=False, reason=failure_reason)
            self._run_freshness[series_id] = SeriesFreshness(
                series_id=series_id, status=FRESHNESS_EMPTY,
                detail="fetch failed — no observation returned",
            )
            return pd.Series(dtype=float)

        # A response came back (possibly empty) without a transport
        # exception — resets the outage breaker either way. Mirrors
        # pre-existing behavior: only a transport failure counts as an
        # outage signal, not a data-availability oddity.
        self._consecutive_failed_series = 0
        if result is None or len(result) == 0:
            # FRED responded successfully but returned 0 rows. Distinct from
            # the exception path (logged above) — usually a misconfigured
            # series_id, a discontinued series, or temporarily missing
            # observation_start window. Surface so macro_analyst's
            # `staleness_days: None` is actionable instead of opaque.
            logger.warning(
                "FRED returned 0 observations for %s (kwargs=%s) — "
                "regime detection will see None freshness",
                series_id, kwargs,
            )
            self._note_coverage(series_id, ok=False, reason="zero_observations")
            self._run_freshness[series_id] = SeriesFreshness(
                series_id=series_id, status=FRESHNESS_EMPTY,
                detail="FRED returned zero observations",
            )
            return pd.Series(dtype=float)
        self._note_coverage(series_id, ok=True, reason="")
        # Freshness is classified on the RAW series (NaN rows included) —
        # see _record_freshness. Callers `.dropna()` for values afterwards;
        # this must run before that, on what FRED actually sent.
        self._record_freshness(series_id, result)
        return result

    @staticmethod
    def _staleness_days(series: pd.Series) -> int | None:
        """Business days between the latest observation and today. None if series empty.

        DESCRIPTIVE ONLY as of 2026-09-11: this is how OLD the reading is,
        and age is no longer a test of anything. No gate reads it — the
        regime-shift and confidence gates in
        `src/agents/macro_analyst.py::_apply_sanity_checks` now read
        `freshness` (see `SeriesFreshness`), which asks whether the reading
        is the latest that exists and whether a newer one is overdue. This
        number is kept because it is genuinely informative context for the
        seat's own reasoning ("the current CPI print is 36 days old" is a
        fact worth stating); it must not become a threshold again.

        None always means "no data at all" (FRED returned 0 rows) — never
        means "data exists but freshness unknown". _safe_get_series logs
        a WARNING on the empty-series path so the operator can see why a
        downstream staleness_days came back None.

        "Today" is the ET trading-day date — not the host-local date. CLAUDE.md
        invariant: any host TZ must produce the same data. Using `date.today()`
        here previously caused SGT-resident operators running before ET cutoff
        to see staleness ±1 day off vs the same data viewed from ET.
        """
        if series.empty:
            return None
        try:
            latest = pd.Timestamp(series.index[-1]).normalize()
            today = pd.Timestamp(et_today())
            # Business days, as the docstring promises (audit round 2:
            # calendar days made every Monday read "3 days stale" and
            # spuriously tripped the macro staleness sanity check after
            # each weekend/holiday).
            import numpy as _np
            return max(0, int(_np.busday_count(
                latest.date(), today.date(),
            )))
        except Exception:
            return None

    def get_vix(self, lookback_days: int = 30) -> dict:
        series = self._safe_get_series(
            "VIXCLS",
            observation_start=_et_lookback_start(lookback_days),
        )
        series = series.dropna()
        if series.empty:
            return {
                "current": None, "mean_5d": None, "trend": "unknown",
                "staleness_days": None,
                **self._freshness_fields("VIXCLS"),
            }
        current = float(series.iloc[-1])
        mean_5d = float(series.tail(5).mean())
        if len(series) >= 5:
            prev = float(series.iloc[-5])
            trend = "rising" if current > prev else "falling" if current < prev else "flat"
        else:
            trend = "unknown"
        return {
            "current": current,
            "mean_5d": mean_5d,
            "trend": trend,
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("VIXCLS"),
        }

    def get_treasury_yields(self) -> dict:
        """2Y/10Y curve (existing) plus 3M/10Y (DGS3MO, added Phase 4.2).

        The 3-month/10-year spread is the curve academic recession research
        (Estrella & Mishkin; the NY Fed's own recession-probability model)
        actually uses — it has historically inverted earlier and with fewer
        false positives than 2Y/10Y. Both spreads are reported side by
        side rather than one replacing the other: they can disagree (3m/10y
        inverted while 2y/10y is not, or vice versa), and that disagreement
        is itself informative about where along the curve the market is
        pricing near-term Fed action versus longer-run growth/inflation.
        """
        us3mo_series = self._safe_get_series(
            "DGS3MO",
            observation_start=_et_lookback_start(14),
        ).dropna()
        us2y_series = self._safe_get_series(
            "DGS2",
            observation_start=_et_lookback_start(14),
        ).dropna()
        us10y_series = self._safe_get_series(
            "DGS10",
            observation_start=_et_lookback_start(14),
        ).dropna()
        us3mo = float(us3mo_series.iloc[-1]) if not us3mo_series.empty else None
        us2y = float(us2y_series.iloc[-1]) if not us2y_series.empty else None
        us10y = float(us10y_series.iloc[-1]) if not us10y_series.empty else None
        spread = (us10y - us2y) if us2y is not None and us10y is not None else None
        spread_3m_10y = (us10y - us3mo) if us3mo is not None and us10y is not None else None
        staleness = self._staleness_days(us10y_series if not us10y_series.empty else us2y_series)
        return {
            "us3mo": us3mo,
            "us2y": us2y,
            "us10y": us10y,
            "spread_2_10": round(spread, 4) if spread is not None else None,
            "inverted": spread < 0 if spread is not None else None,
            "spread_3m_10y": round(spread_3m_10y, 4) if spread_3m_10y is not None else None,
            "inverted_3m_10y": spread_3m_10y < 0 if spread_3m_10y is not None else None,
            "staleness_days": staleness,
            **self._freshness_fields("DGS10", "DGS2", "DGS3MO"),
        }

    def get_fed_funds_rate(self) -> dict:
        """Daily effective fed funds rate (DFF), not the monthly FEDFUNDS.

        DFF updates every business day, so rate cuts/hikes and policy shifts show
        up within 24 hours instead of at month-end.
        """
        series = self._safe_get_series(
            "DFF",
            observation_start=_et_lookback_start(30),
        ).dropna()
        if series.empty:
            return {
                "current": None, "change_30d": None, "staleness_days": None,
                **self._freshness_fields("DFF"),
            }
        current = float(series.iloc[-1])
        change_30d = float(current - series.iloc[0]) if len(series) >= 2 else 0.0
        return {
            "current": current,
            "change_30d": round(change_30d, 4),
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("DFF"),
        }

    def get_inflation(self) -> dict:
        """Headline (CPIAUCSL) and core (CPILFESL) CPI — monthly series.

        Returns latest YoY % and MoM % for each, plus PCE (PCEPI) for the Fed's preferred gauge.
        """
        def _latest_yoy_mom(series_id: str) -> tuple[float | None, float | None, pd.Series]:
            s = self._safe_get_series(
                series_id,
                observation_start=_et_lookback_start(500),
            ).dropna()
            if len(s) < 13:
                return None, None, s
            yoy = float((s.iloc[-1] / s.iloc[-13] - 1) * 100)
            mom = float((s.iloc[-1] / s.iloc[-2] - 1) * 100) if len(s) >= 2 else None
            return round(yoy, 2), round(mom, 2) if mom is not None else None, s

        headline_yoy, headline_mom, headline_series = _latest_yoy_mom("CPIAUCSL")
        core_yoy, core_mom, _ = _latest_yoy_mom("CPILFESL")
        pce_yoy, _, _ = _latest_yoy_mom("PCEPI")
        return {
            "headline_cpi_yoy": headline_yoy,
            "headline_cpi_mom": headline_mom,
            "core_cpi_yoy": core_yoy,
            "core_cpi_mom": core_mom,
            "pce_yoy": pce_yoy,
            "staleness_days": self._staleness_days(headline_series),
            **self._freshness_fields("CPIAUCSL", "CPILFESL", "PCEPI"),
        }

    def get_unemployment(self) -> dict:
        """Unemployment rate (UNRATE) — monthly.

        Returns current level, 3-month change, and 12-month change. Rising unemployment
        is a classic late-cycle / risk-off signal (Sahm rule: +0.5pp in 3m ≈ recession).
        """
        series = self._safe_get_series(
            "UNRATE",
            observation_start=_et_lookback_start(500),
        ).dropna()
        if series.empty:
            return {
                "current": None, "change_3m": None, "change_12m": None,
                "staleness_days": None,
                **self._freshness_fields("UNRATE"),
            }
        current = float(series.iloc[-1])
        change_3m = float(current - series.iloc[-4]) if len(series) >= 4 else None
        change_12m = float(current - series.iloc[-13]) if len(series) >= 13 else None
        return {
            "current": round(current, 2),
            "change_3m": round(change_3m, 2) if change_3m is not None else None,
            "change_12m": round(change_12m, 2) if change_12m is not None else None,
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("UNRATE"),
        }

    def get_credit_spread(self) -> dict:
        """High-yield OAS (ICE BofA HY index, BAMLH0A0HYM2) — daily.

        Wider HY OAS = credit stress rising = risk-off signal. Historical ranges:
        < 300bps  = very benign, late cycle
        300-450   = normal
        450-600   = elevated, pay attention
        > 600     = stress, recession-like
        """
        series = self._safe_get_series(
            "BAMLH0A0HYM2",
            observation_start=_et_lookback_start(60),
        ).dropna()
        if series.empty:
            return {
                "current_bps": None, "change_30d_bps": None, "staleness_days": None,
                **self._freshness_fields("BAMLH0A0HYM2"),
            }
        current = float(series.iloc[-1]) * 100  # FRED returns % — convert to bps
        # Anchor the reference to a DATE, not to the head of the window.
        #
        # 2026-07-16 audit: `series.iloc[0]` is the OLDEST observation in a
        # 60-CALENDAR-day fetch, so "change_30d_bps" was really a ~57-60 day
        # change — about 2x the advertised window, and on a live check it even
        # flipped the sign (code said -11.0 bps; the true 30-day change was
        # +6.0 bps). BAMLH0A0HYM2 is business-daily; keep the 60d fetch as
        # buffer for holidays/gaps, but take the last observation at or before
        # T-30d. (The wide window was inherited verbatim from the earlier
        # MONTHLY FEDFUNDS fetcher, where iloc[0] was harmless.)
        prior_30d = current
        if len(series) >= 2:
            cutoff = series.index[-1] - pd.Timedelta(days=30)
            prior = series[series.index <= cutoff]
            prior_30d = float(prior.iloc[-1] if not prior.empty else series.iloc[0]) * 100
        return {
            "current_bps": round(current, 1),
            "change_30d_bps": round(current - prior_30d, 1),
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("BAMLH0A0HYM2"),
        }

    # --- Phase 4.2 additions ------------------------------------------------
    # docs/AGENT_ROLE_AUDIT.md §2.3 named five missing free FRED series;
    # ICSA is a sixth added alongside them for the same reason (a stale
    # monthly-only labor read). All six verified live against FRED
    # (real network call, real API key) before being wired in here — see
    # the Phase 4.2 fetch-verification report.

    def get_real_yield_and_breakeven(self) -> dict:
        """10-year real yield (DFII10, TIPS-implied) and 10-year breakeven
        inflation (T10YIE) — daily.

        Nominal DGS10 conflates two different markets: it moves when REAL
        growth/rate expectations shift AND when INFLATION expectations
        shift, and a bare nominal-yield reading cannot tell which moved.
        DFII10 isolates the real (growth/policy) component; T10YIE — FRED's
        own DGS10-minus-DFII10 series, not re-derived here — isolates the
        inflation-expectations component. Reading them TOGETHER is new
        capability: a rising nominal 10Y with a flat/falling breakeven is a
        growth/real-rate story (tightening financial conditions); a rising
        nominal 10Y with a rising breakeven is an inflation story
        (re-acceleration fear) — the desk previously could not distinguish
        these from DGS10 alone.
        """
        real_series = self._safe_get_series(
            "DFII10",
            observation_start=_et_lookback_start(30),
        ).dropna()
        breakeven_series = self._safe_get_series(
            "T10YIE",
            observation_start=_et_lookback_start(30),
        ).dropna()
        real = float(real_series.iloc[-1]) if not real_series.empty else None
        breakeven = float(breakeven_series.iloc[-1]) if not breakeven_series.empty else None
        staleness = self._staleness_days(
            real_series if not real_series.empty else breakeven_series
        )
        return {
            "real_10y": round(real, 4) if real is not None else None,
            "breakeven_10y": round(breakeven, 4) if breakeven is not None else None,
            "staleness_days": staleness,
            **self._freshness_fields("DFII10", "T10YIE"),
        }

    def get_dollar_index(self) -> dict:
        """Trade-weighted USD strength (DTWEXBGS, the Fed's Nominal Broad
        Dollar Index) — daily, but published with roughly a one-week lag
        versus the other daily series here (live-verified: latest print was
        ~7 business days behind DGS10/VIXCLS on the same check) — that lag
        is normal H.10-release cadence, not staleness in the sense the
        other daily series use, so the macro analyst prompt treats it with
        the same "weekly-cadence" staleness tolerance as ICSA below.

        Dollar strength is a macro input independent of everything else
        fetched here: a strong dollar is a headwind for US multinational
        earnings (FX translation) and often signals policy divergence or
        risk-off demand for USD; dollar weakness tends to support commodity
        prices and EM/cyclical risk appetite.
        """
        series = self._safe_get_series(
            "DTWEXBGS",
            observation_start=_et_lookback_start(60),
        ).dropna()
        if series.empty:
            return {
                "current": None, "change_30d": None, "staleness_days": None,
                **self._freshness_fields("DTWEXBGS"),
            }
        current = float(series.iloc[-1])
        # Same date-anchored-change fix as get_credit_spread (2026-07-16
        # audit) — anchor to a DATE at or before T-30d, not to the head of
        # a fixed-length window, which for a longer buffer window would
        # silently measure a wider span than advertised.
        change_30d = 0.0
        if len(series) >= 2:
            cutoff = series.index[-1] - pd.Timedelta(days=30)
            prior = series[series.index <= cutoff]
            prior_val = float(prior.iloc[-1]) if not prior.empty else float(series.iloc[0])
            change_30d = current - prior_val
        return {
            "current": round(current, 3),
            "change_30d": round(change_30d, 3),
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("DTWEXBGS"),
        }

    def get_ig_credit_spread(self) -> dict:
        """Investment-grade OAS (ICE BofA US Corporate Index, BAMLC0A0CM) —
        daily, companion to the existing HY OAS (BAMLH0A0HYM2).

        HY OAS reflects stress specific to junk-rated issuers and can stay
        tight even when broader corporate credit is under pressure — IG
        issuers are a larger, more systemically-linked slice of the credit
        market. IG spreads widening while HY stays tight is itself a
        distinct signal (stress concentrating in higher-quality balance
        sheets / funding markets) from the reverse (junk-specific stress
        with IG calm) — the desk previously had no way to tell these apart.
        """
        series = self._safe_get_series(
            "BAMLC0A0CM",
            observation_start=_et_lookback_start(60),
        ).dropna()
        if series.empty:
            return {
                "current_bps": None, "change_30d_bps": None, "staleness_days": None,
                **self._freshness_fields("BAMLC0A0CM"),
            }
        current = float(series.iloc[-1]) * 100  # FRED returns % — convert to bps
        prior_30d = current
        if len(series) >= 2:
            cutoff = series.index[-1] - pd.Timedelta(days=30)
            prior = series[series.index <= cutoff]
            prior_30d = float(prior.iloc[-1] if not prior.empty else series.iloc[0]) * 100
        return {
            "current_bps": round(current, 1),
            "change_30d_bps": round(current - prior_30d, 1),
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("BAMLC0A0CM"),
        }

    def get_jobless_claims(self) -> dict:
        """Initial jobless claims (ICSA) — weekly.

        UNRATE (already fetched) is monthly and, per the existing staleness
        discipline in this module and macro_analyst.md, is only as fresh as
        the last BLS release — often 20-51 business days by construction.
        ICSA publishes weekly (Thursdays, for the week ending the prior
        Saturday) and is one of the most-watched high-frequency labor
        signals precisely because it leads UNRATE by weeks: a real-time
        read on layoffs versus UNRATE's stale monthly snapshot.
        """
        series = self._safe_get_series(
            "ICSA",
            observation_start=_et_lookback_start(90),
        ).dropna()
        if series.empty:
            return {
                "current": None, "change_4w": None, "trend": "unknown",
                "staleness_days": None,
                **self._freshness_fields("ICSA"),
            }
        current = float(series.iloc[-1])
        change_4w = None
        trend = "unknown"
        if len(series) >= 5:
            prior = float(series.iloc[-5])
            change_4w = current - prior
            trend = "rising" if current > prior else "falling" if current < prior else "flat"
        return {
            "current": current,
            "change_4w": round(change_4w, 0) if change_4w is not None else None,
            "trend": trend,
            "staleness_days": self._staleness_days(series),
            **self._freshness_fields("ICSA"),
        }

    def get_macro_summary(self) -> dict:
        """Fetch all fifteen configured FRED series and return the payload
        the macro analyst reads.

        Resets the per-call resilience state (fetch deadline, coverage
        counters) FIRST, then fetches every series, then snapshots the
        result into `self.last_coverage` (a MacroCoverage) — the pipeline
        reads that side channel right after calling this method to set
        data_status["macro"]/thread coverage into the analyst's prompt. See
        MacroCoverage's docstring for why this is a side channel rather
        than a change to this method's own (widely-consumed) return shape.
        """
        self._deadline = time.monotonic() + self.total_fetch_deadline_s
        self._run_configured = 0
        self._run_succeeded = 0
        self._run_failed = []
        self._run_freshness = {}
        # Metadata is only valid until the next print lands, so it is never
        # carried across calls.
        self._series_info_cache = {}
        try:
            summary = {
                "vix": self.get_vix(),
                "treasury": self.get_treasury_yields(),
                "fed_funds_rate": self.get_fed_funds_rate(),
                "inflation": self.get_inflation(),
                "unemployment": self.get_unemployment(),
                "credit_spread": self.get_credit_spread(),
                "real_rates": self.get_real_yield_and_breakeven(),
                "dollar_index": self.get_dollar_index(),
                "ig_credit_spread": self.get_ig_credit_spread(),
                "jobless_claims": self.get_jobless_claims(),
            }
            overdue = [
                f for f in self._run_freshness.values()
                if f.status == FRESHNESS_OVERDUE
            ]
            if overdue:
                logger.error(
                    "FRED prints OVERDUE this run — a newer reading is past "
                    "due by each series' own cadence and publication lag: %s",
                    "; ".join(f.describe() for f in overdue),
                )
            self.last_coverage = MacroCoverage(
                configured=self._run_configured,
                succeeded=self._run_succeeded,
                failed=list(self._run_failed),
                overdue=overdue,
            )
            return summary
        finally:
            # Scoped to this one call — a later direct get_vix()/etc. call
            # (outside get_macro_summary()) must not inherit a stale,
            # already-expired deadline from a previous run.
            self._deadline = None
