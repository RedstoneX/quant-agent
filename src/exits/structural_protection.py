"""The structural-protection check for one holding: whether the level the thesis rests on still holds on the chart.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
from src.trading_calendar import et_today
from src.sentinel.guarded import record_guarded_pass

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class StructuralProtection:
    """The structural-protection check for one holding: whether the level the thesis rests on still holds on the chart."""

    def __init__(self, *,
                 voice_structural_protection_break,
                 config,
                 db,
                 market,
                 risk_engine) -> None:
        self._voice_structural_protection_break = voice_structural_protection_break
        self.config = config
        self.db = db
        self.market = market
        self.risk_engine = risk_engine

    def _entry_date_for_noise_band(self, symbol: str, entry_date: str | None) -> str | None:
        """The ISO session the position was opened, for the noise band's
        running-extreme anchor only.

        Prefers what the caller already holds; otherwise reads the symbol's
        last buy, the same row `_opened_today` reads its date from. Any
        failure is None, which leaves the band ENTRY-anchored rather than
        taking the extreme over a made-up window.
        """
        if isinstance(entry_date, str) and entry_date.strip():
            return entry_date.strip()[:10]
        try:
            row = self.db.get_symbol_last_buy(symbol) or {}
        except Exception as e:  # noqa: BLE001
            record_guarded_pass(self.db, "structural_protection.entry_date_for_noise_band", e)
            logger.warning(
                "structural protection: no entry date for %s (%s) — the "
                "noise band stays anchored on entry price", symbol, e,
            )
            return None
        ts = str((row or {}).get("timestamp") or "")[:10]
        return ts or None

    def _extreme_since_entry(
        self, symbol: str, sorted_bars: list, entry_date: str | None, *, is_short: bool,
    ) -> float | None:
        """The position's RUNNING EXTREME since entry — highest high for a
        long, lowest low for a short — over the daily bars from the entry
        session onward, or None when it cannot be read.

        This is the noise band's anchor in `check_structural_protection`'s
        fallback (MEASURED 2026-10-04: 54 blocks removed, none added). It is
        computable ONLY from a real entry DATE: with no entry session there is
        no honest window to take an extreme over, and picking a lookback would
        be exactly the invented number this desk refuses. None then means "no
        extreme", and the band falls back to the entry anchor with the
        recorded row saying `anchor_kind=entry`. Never raises.
        """
        ent_date = self._entry_date_for_noise_band(symbol, entry_date)
        if not ent_date or not sorted_bars:
            return None
        try:
            vals = []
            for b in sorted_bars:
                if str(getattr(b, "date", "")) [:10] < ent_date:
                    continue
                v = b.low if is_short else b.high
                if v is None:
                    continue
                v = float(v)
                if v == v and v not in (float("inf"), float("-inf")) and v > 0:
                    vals.append(v)
        except (TypeError, ValueError) as e:
            logger.warning(
                "structural protection: unreadable bar extreme for %s (%s) — "
                "the noise band stays anchored on entry price", symbol, e,
            )
            return None
        if not vals:
            return None
        return min(vals) if is_short else max(vals)

    def _structural_protection_for_holding(
        self,
        *,
        symbol: str,
        thesis_invalid_if: str | None,
        entry_price: float | None,
        stop_loss: float | None,
        is_short: bool,
        run_id: str,
        persist: bool = True,
        entry_date: str | None = None,
    ):
        """Fresh, close-based structural-protection read for one held
        position, including the cross-day confirmation lookup and the
        persist of today's read for the NEXT trading day to confirm
        against. Returns the `StructuralProtectionCheck`.

        `persist=False` makes the call READ-ONLY: the cross-day lookup
        still runs, but today's `raw_broken` is not filed, so this read can
        never become the prior-day half of a future confirmation. Callers
        that are consulting the check purely for the audit trail must pass
        it. Filing a break from a NEW call site would let a break confirm a
        day earlier than it does today, which lifts `protected` a day
        earlier, which can turn a currently-BLOCKED holding-discipline exit
        into an allowed one on the following session — a loosening, by
        side-effect, of a gate this repo deliberately keeps tight
        (docs/WORK.md item 60).

        `entry_date` (ISO `YYYY-MM-DD`) is the session the position was
        opened, and it exists for ONE reason: the noise-band fallback at the
        bottom of `check_structural_protection` is anchored on the position's
        RUNNING EXTREME since entry, which cannot be computed from entry
        PRICE alone. Given it, the extreme is read off the daily bars this
        method already fetched (highest high for a long, lowest low for a
        short, entry session included). Omitted or unreadable, it is looked
        up from the symbol's last buy, and failing that the fallback stays
        ENTRY-anchored — the pre-2026-10-04 behaviour, never a guessed
        lookback window. MEASURED 2026-10-04: re-anchoring this home removes
        54 blocks of 383 and adds none. The midday position reviewer is NOT
        re-anchored (there it is a measured regression) and is untouched.

        Spec item 25 (2026-09-03/04, corrected same day) — replaces the
        flat `days_held < 5` holding-discipline window with a data-driven
        one: `src.risk.exit_guard.check_structural_protection`. That
        function is pure; this method supplies it with real numbers
        recomputed straight from bars using the SAME deterministic, no-LLM
        machinery `TechAnalystAgent.analyze_batch` uses when a position is
        first bought (`compute_indicators` for ATR/MAs,
        `find_structural_levels` for the structural levels) — just run
        again here against a HELD position instead of a BUY candidate, on
        the same `config.trading.lookback_days` window so level detection
        sees exactly the bar count it was tuned against.

        DELIBERATELY uses the latest COMPLETED DAILY CLOSE from those same
        bars (`bars[-1].close`), never a live broker/quote price — real
        technical-analysis practice (and `check_structural_protection`'s
        own confirmation gate) requires a level to break on a CLOSE, not an
        intraday tick, or a routine intrabar wick would misread as an
        invalidated thesis.

        The cross-day lookup is keyed off THIS READ's own `bar_date` (the
        actual latest completed close, which may be a prior calendar day if
        the market is still open) rather than wall-clock "today" — several
        same-session pipeline cycles reading the SAME close must never be
        miscounted as two separate confirming trading days.

        Never raises: a bars/indicator failure degrades to no ATR/levels/
        close, which `check_structural_protection` already treats as "no
        qualifying basis" and falls back to its noise-band check on — never
        to a wrong verdict. The DB read/write around it are each wrapped
        separately so a memory hiccup degrades to "unconfirmed" /
        "unpersisted" rather than losing the whole check.
        """
        from src.data.levels import CLUSTER_TOLERANCE_PCT
        from src.risk.exit_guard import check_structural_protection
        from src.trading_calendar import et_today

        computed_levels: list[float] = []
        computed_level_touches: dict[float, int] = {}
        computed_level_zones: dict[float, list[float]] = {}
        computed_level_bars: dict[float, list[tuple[float, float]]] = {}
        atr = ma_20 = ma_50 = ma_200 = ma_200_prior = adx = close_price = bar_date = None
        extreme_since_entry: float | None = None
        # Held outside the try below so the running-extreme anchor survives an
        # indicator/level failure: the bars are sorted before any of that runs,
        # and the extreme needs nothing else.
        sorted_bars: list = []
        # The completed trading sessions strictly before today's close, most
        # recent first, taken from THIS position's own daily bars — the
        # authoritative trading calendar (weekends/holidays already removed).
        # The exit guard uses it to enforce that only CONSECUTIVE prior sessions
        # count toward break confirmation (a gap resets — #3).
        prior_session_dates: list[str] = []
        try:
            bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days) or []
            if bars:
                from src.data.levels import find_structural_levels
                from src.data.technical import compute_indicators
                sorted_bars = sorted(bars, key=lambda b: b.date)
                last_bar = sorted_bars[-1]
                close_price = float(last_bar.close)
                bar_date = str(last_bar.date)
                prior_session_dates = [
                    str(b.date) for b in reversed(sorted_bars) if str(b.date) < bar_date
                ]
                indicators = compute_indicators(symbol, bars)
                atr = indicators.atr_14
                ma_20, ma_50, ma_200 = indicators.ma_20, indicators.ma_50, indicators.ma_200
                ma_200_prior = indicators.ma_200_prior
                adx = indicators.adx_14
                supports, resistances = find_structural_levels(bars)
                all_levels = (*supports, *resistances)
                computed_levels = sorted(lv.price for lv in all_levels)
                computed_level_touches = {lv.price: lv.touches for lv in all_levels}
                computed_level_zones = {
                    lv.price: [float(lv.zone_low), float(lv.zone_high)]
                    for lv in all_levels
                    if lv.zone_low is not None and lv.zone_high is not None
                }
                computed_level_bars = {
                    lv.price: list(lv.pivot_bars) for lv in all_levels
                }
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: bars/indicator fetch failed for %s "
                "(%s) — checking with no close/level/MA data (falls back "
                "to the noise-band check)", symbol, e,
            )
        # A bars-fetch failure leaves no real close date; fall back to
        # wall-clock today purely as a persistence key — harmless because
        # `raw_broken` is always False on the no-close path below, so it can
        # never manufacture a false confirmation regardless of the date
        # it's filed under.
        effective_bar_date = bar_date or str(et_today())

        # THE NOISE BAND'S ANCHOR for this home (2026-10-04). Computed from
        # the bars already fetched above; None leaves the band entry-anchored.
        extreme_since_entry = self._extreme_since_entry(
            symbol, sorted_bars, entry_date, is_short=is_short,
        )

        # Read the per-session break RECORDS for this position (most recent
        # first), so the exit guard can reconstruct the CONSECUTIVE-confirming-
        # close streak with adjacency (#3) and margin-consistency (#4). The
        # exclude_run_id guard keeps several same-session cycles reading one
        # close from double-counting it.
        prior_break_records: list = []
        try:
            prior_break_records = self.db.get_recent_holding_protection_breaks(
                symbol, before_bar_date=effective_bar_date, exclude_run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: prior-close read failed for %s "
                "(%s) — today's break, if any, starts unconfirmed", symbol, e,
            )

        # Same two ratified bars `PortfolioConstructor`'s `ConstructorConfig`
        # mirrors off `self.risk_engine.config` (see its own "Kept in sync
        # with risk.*" comments) — read defensively rather than assumed,
        # since this method must survive a lightweight pipeline double (unit
        # tests) that never built a real `risk_engine`. The fallback values
        # are the RiskConfig field defaults themselves, not a second
        # invented number.
        risk_cfg = getattr(self, "risk_engine", None)
        risk_cfg = getattr(risk_cfg, "config", None)
        min_level_touches = getattr(risk_cfg, "min_level_touches_for_stop_honor", 5)

        check = check_structural_protection(
            thesis_invalid_if=thesis_invalid_if,
            current_price=close_price,
            entry_price=entry_price,
            stop_loss=stop_loss,
            atr=atr,
            is_short=is_short,
            computed_levels=computed_levels,
            computed_level_touches=computed_level_touches,
            computed_level_zones=computed_level_zones,
            computed_level_bars=computed_level_bars,
            min_level_touches=min_level_touches,
            # NOT a setting and not a fallback default — this is the exact
            # constant `find_structural_levels` used to cluster pivots into
            # the zones being matched against, so the tolerance cannot be
            # anything else. docs/WORK.md item 46.
            level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
            ma_20=ma_20, ma_50=ma_50, ma_200=ma_200, ma_200_prior=ma_200_prior,
            adx=adx,
            prior_break_records=prior_break_records,
            prior_session_dates=prior_session_dates,
            extreme_since_entry=extreme_since_entry,
        )

        try:
            if persist:
                self.db.save_holding_protection_break(
                    run_id=run_id, symbol=symbol, raw_broken=check.raw_broken,
                    bar_date=effective_bar_date, close=close_price,
                    basis=check.basis, detail=check.detail,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structural protection: failed to persist today's read for "
                "%s (%s) — the next trading day's confirmation check will "
                "start unconfirmed for it", symbol, e,
            )

        # VOICE THE WHY (owner mandate 2026-09-24). When this gate reaches a
        # DECISIVE break outcome — a confirmed break that clears the desk to
        # exit, or a break held through pending confirmation — push the plain-
        # language reason to BOTH owner surfaces via the mechanisms the desk
        # already uses for exactly this: `notifier.send_owner_alert` for the
        # Telegram alert, and a durable `specialist_evidence` row (which the
        # board journal / Mission Control read) for the dashboard. Only on a
        # persisting read (a real exit-decision or rotation-eligibility read,
        # not a purely advisory replay) and deduplicated per run+symbol so the
        # several pipeline cycles in one session reading the same close do not
        # re-alert. Best-effort by construction — a voicing failure never
        # affects the protection verdict itself.
        if persist and check.owner_reason:
            self._voice_structural_protection_break(
                symbol=symbol, run_id=run_id, check=check,
            )

        return check
