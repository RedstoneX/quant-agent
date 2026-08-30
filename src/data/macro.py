import logging
import random
import socket
import time
from dataclasses import dataclass

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

    @property
    def failed_count(self) -> int:
        return len(self.failed)

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
        if not self.failed:
            return (
                f"Macro coverage: {self.succeeded}/{self.configured} FRED "
                f"series returned data. Full coverage."
            )
        names = ", ".join(f"{f.series_id} ({f.reason})" for f in self.failed)
        return (
            f"Macro coverage: {self.succeeded}/{self.configured} FRED series "
            f"returned data this run. FAILED: {names}. Treat this as a "
            f"coverage GAP, not a confirmed reading — a missing indicator is "
            f"not evidence that indicator is calm."
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
            return pd.Series(dtype=float)
        self._note_coverage(series_id, ok=True, reason="")
        return result

    @staticmethod
    def _staleness_days(series: pd.Series) -> int | None:
        """Business days between the latest observation and today. None if series empty.

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
            return {"current": None, "mean_5d": None, "trend": "unknown", "staleness_days": None}
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
            return {"current": None, "change_30d": None, "staleness_days": None}
        current = float(series.iloc[-1])
        change_30d = float(current - series.iloc[0]) if len(series) >= 2 else 0.0
        return {
            "current": current,
            "change_30d": round(change_30d, 4),
            "staleness_days": self._staleness_days(series),
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
            return {"current": None, "change_3m": None, "change_12m": None, "staleness_days": None}
        current = float(series.iloc[-1])
        change_3m = float(current - series.iloc[-4]) if len(series) >= 4 else None
        change_12m = float(current - series.iloc[-13]) if len(series) >= 13 else None
        return {
            "current": round(current, 2),
            "change_3m": round(change_3m, 2) if change_3m is not None else None,
            "change_12m": round(change_12m, 2) if change_12m is not None else None,
            "staleness_days": self._staleness_days(series),
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
            return {"current_bps": None, "change_30d_bps": None, "staleness_days": None}
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
            return {"current": None, "change_30d": None, "staleness_days": None}
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
            return {"current_bps": None, "change_30d_bps": None, "staleness_days": None}
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
            self.last_coverage = MacroCoverage(
                configured=self._run_configured,
                succeeded=self._run_succeeded,
                failed=list(self._run_failed),
            )
            return summary
        finally:
            # Scoped to this one call — a later direct get_vix()/etc. call
            # (outside get_macro_summary()) must not inherit a stale,
            # already-expired deadline from a previous run.
            self._deadline = None
