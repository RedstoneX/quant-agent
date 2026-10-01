"""Prompt-fact family: the PM's view of the book it holds: per-position facts, stop coverage, capital at risk, the account's recent performance and the PM facts block.

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210: one of
the six fact families split out of `PromptFacts`. Method bodies are the
former `PromptFacts` bodies byte for byte; this class takes ONLY the
7 collaborators those bodies read. Nothing here may import
`src.pipeline` or `src.pipeline_prompt_facts`.
"""

import json as _json
from src.pipeline_context import PMFacts
from src.risk.metrics import drift_flag as _drift_flag_check
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import peak_to_trough_pct, position_weight_pct
from src.trading_calendar import et_today, session_date_key
from src.prompt_facts_ports import _ABSENT, bind_ports, logger


#: Ceiling on how many symbols get a company-profile lookup for PM's facts
#: block. Profiles are 30-day-cached, so this only bites on a cold cache —
#: but on a cold cache it is one network round trip per symbol, and a
#: pathological candidate list must not be able to turn a nice-to-have
#: identity block into the longest step of the morning session.
_PM_PROFILE_SYMBOL_CAP = 40


class BookFacts:
    """The PM's view of the book it holds: per-position facts, stop coverage, capital at risk, the account's recent performance and the PM facts block."""

    def __init__(
        self,
        *,
        db=_ABSENT,
        broker=_ABSENT,
        tech_store=_ABSENT,
        config=_ABSENT,
        sweeper=_ABSENT,
        parse_logged_agent_response=_ABSENT,
        atr_for_symbol=_ABSENT,
    ) -> None:
        bind_ports(self, {
            "db": db,
            "broker": broker,
            "tech_store": tech_store,
            "config": config,
            "_sweeper": sweeper,
            "_parse_logged_agent_response": parse_logged_agent_response,
            "_atr_for_symbol": atr_for_symbol,
        })

    def _build_position_history(self, positions) -> dict[str, dict]:
        """L2 memory: for each held symbol, entry context + Tech rating trajectory.

        PM uses this to anchor 'when did I buy + why' and recognize when a fresh
        setup has been maturing vs stuck vs invalidated.
        """
        from datetime import date as _date
        from src.execution.stop_records import recorded_initial_stop
        out: dict[str, dict] = {}
        today = et_today()
        for p in positions:
            sym = p.symbol
            entry = None
            try:
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                opening = "SHORT" if qty < 0 else "BUY"
                entry = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:
                logger.warning("position_history: last_buy lookup failed for %s: %s", sym, e)

            entry_date_str = None
            days_held: int | None = None
            if entry and entry.get("timestamp"):
                try:
                    ts = entry["timestamp"]
                    entry_date = _date.fromisoformat(ts[:10]) if isinstance(ts, str) else None
                    if entry_date is not None:
                        entry_date_str = str(entry_date)
                        days_held = max(0, (today - entry_date).days)
                except (ValueError, TypeError):
                    pass

            try:
                tech_history = self.tech_store.get_history(sym, days=7)
            except Exception as e:
                logger.warning("position_history: tech history failed for %s: %s", sym, e)
                tech_history = []

            out[sym] = {
                "entry_date": entry_date_str,
                "entry_price": entry.get("price") if entry else None,
                "entry_reasoning": (entry.get("reasoning") or "")[:280] if entry else "",
                # Real, untruncated falsifier condition — see
                # TradeDecision.thesis_invalid_if in models.py. Carried
                # ALONGSIDE entry_reasoning (never a replacement for it):
                # the embedded "(invalid if: ...)"/"(thesis_invalid_if: ...)"
                # text above is truncated at 280 chars here (and 500 chars
                # upstream in the constructor), which could silently cut off
                # a long condition. This column is None for legacy rows
                # written before the trades.thesis_invalid_if column existed.
                "thesis_invalid_if": entry.get("thesis_invalid_if") if entry else None,
                "days_held": days_held,
                "tech_history": tech_history,
                # The stop recorded at entry — RiskStage's structural
                # holding-discipline check (spec item 25) reads this to find
                # the level backing it. Deliberately the frozen entry-time
                # stop (`initial_stop_loss`), not the live `stop_loss` a
                # trail may have since written back, and not the live
                # broker stop: same "the bet that was actually made"
                # reasoning `_build_position_facts.initial_stop` documents.
                "stop_loss": (
                    recorded_initial_stop(entry) or None
                ) if entry else None,
            }
        return out

    def _build_stop_map(self, positions) -> tuple[dict[str, float], dict[str, float]]:
        """`(live_stops, initial_stops)` keyed by symbol.

        Live stops are broker truth (already trailed). Initial stops come from
        the last executed BUY row and are what an R-multiple's denominator must
        use — the bet that was actually made, not the one it was ratcheted to.
        A symbol missing from `live_stops` is genuinely unprotected and
        `portfolio_heat` charges it at full notional; never substitute the BUY
        row's stop for a missing broker stop, because that would report
        protection the account does not have.
        """
        live_stops: dict[str, float] = {}
        initial_stops: dict[str, float] = {}
        for p in positions:
            sym = p.symbol
            try:
                live = self.broker.get_current_stop_price(sym)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: live stop lookup failed for %s: %s", sym, e)
                live = None
            if isinstance(live, (int, float)) and live > 0:
                live_stops[sym] = float(live)
            from src.execution.stop_records import recorded_initial_stop
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                opening = "SHORT" if qty < 0 else "BUY"
                buy = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop map: last-buy lookup failed for %s: %s", sym, e)
                buy = None
            initial = recorded_initial_stop(buy)
            if initial > 0:
                initial_stops[sym] = initial
        return live_stops, initial_stops

    def _build_portfolio_heat(self, positions, total_value: float):
        """Audit §1.3 — total capital at risk, which nothing computed before.

        The cash-equivalent sweep vehicle is excluded rather than counted as
        unprotected: it is deliberately stopless everywhere in this codebase
        and is not a risk position. Returns None on failure so the prompt can
        say "unknown" instead of rendering a confident zero.
        """
        from src.risk.metrics import portfolio_heat
        try:
            sweeper = self._sweeper()
            excluded = set()
            if sweeper is not None and sweeper.symbol:
                excluded.add(str(sweeper.symbol).upper())
            live_stops, initial_stops = self._build_stop_map(positions)
            return portfolio_heat(
                positions=positions,
                equity=total_value,
                stops=live_stops,
                initial_stops=initial_stops,
                exclude_symbols=excluded,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("portfolio heat build failed: %s", e)
            return None

    def _build_pm_facts(
        self,
        *,
        positions: list,
        analyses: list,
        total_value: float,
        cash: float,
        recent_performance: dict,
        macro_analysis=None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
    ) -> PMFacts:
        """Quantitative snapshot surfaced to PM as structured fields.

        Phase 4 #4: reduces PM's reliance on LLM-summarized prose for the
        things that are actually numbers (win rate, sector weights, age
        buckets). Prose layers (weekly_narrative, rm_recent_verdicts)
        stay for qualitative continuity.
        """
        import statistics
        from src.execution.broker import _get_sector as _sector_of

        f = PMFacts()

        # Calibration
        try:
            calib = self.db.compute_trade_calibration(lookback_days=30)
        except Exception as e:
            logger.warning("pm_facts: calibration failed: %s", e)
            calib = {}
        if calib:
            f.closed_trades_30d = int(calib.get("n") or 0)
            f.win_rate_30d_pct = calib.get("win_rate_pct")
            f.avg_return_30d_pct = calib.get("avg_return_pct")
            f.avg_hold_days_30d = calib.get("avg_hold_days")

        # RM discipline
        try:
            rm_rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager", limit=5,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_facts: rm outputs failed: %s", e)
            rm_rows = []
        f.rm_verdicts_seen = len(rm_rows)
        for row in rm_rows:
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            scale = data.get("scale_all_buys", 1.0)
            try:
                if float(scale) < 1.0:
                    f.rm_scale_downs_last5 += 1
            except (TypeError, ValueError):
                pass
            if data.get("modifications"):
                f.rm_mods_last5 += 1

        # Book state.
        #
        # `invested_pct` comes from `book_exposure` — the SAME function the
        # pre-trade gate's `deployment_gap` advisory reads, so PM
        # and RM can no longer be told opposite things about one book (they
        # were: 70% "10pp OVER" to PM and 10% "50pp UNDER" to RM on the same
        # $50k-long/$20k-SQQQ book). `positions` here is already sweep-split
        # by DecisionStage, and `cash` is `deployable_cash` (raw cash + the
        # parked vehicle), so the parked T-bills count as cash on both legs.
        #
        # `net_exposure_pct` is reported ALONGSIDE rather than substituted:
        # deployment answers "is the money at work", direction answers "which
        # way does the book lean", and one number cannot be both.
        from src.risk.rules import book_exposure
        if total_value > 0:
            exposure = book_exposure(positions, total_value)
            f.invested_pct = round(exposure.deployed_pct, 1)
            f.net_exposure_pct = round(exposure.net_pct, 1)
            f.cash_pct = round((cash or 0) / total_value * 100, 1)
        f.position_count = len(positions)

        # Sector weights — SEPARATE long and short budgets (spec §12.2).
        #
        # This REVERSES the netting that shipped with the shorts work: a held
        # short used to add a NEGATIVE weight, so a long 15% and a short 5% in
        # Technology rendered as a single 10% line. Owner's ratified reasoning:
        # *"A long and a short in the same sector is not a hedge... We are
        # trading opportunities."* Netting also showed the PM a smaller number
        # than `RiskRuleEngine.check` enforces against — the PM would reason
        # about concentration from one book while the gate refused on another.
        #
        # `sector_side_weights` is the shared definition the gate and the
        # constructor use, so all three cannot drift apart again. The only
        # thing local here is sector RESOLUTION: PM facts fall back to
        # `_sector_of` when the broker left `Position.sector` blank, and
        # "Unknown" is rendered rather than dropped so the PM can see that a
        # slice of the book is unclassified.
        from src.risk.rules import (
            SECTOR_SIDE_SHORT, sector_side_weights,
        )
        for (sector, side), weight in sector_side_weights(
            positions,
            total_value,
            resolve_sector=lambda p: p.sector or _sector_of(p.symbol) or "Unknown",
            include_unknown=True,
        ).items():
            bucket = (
                f.sector_weights_short if side == SECTOR_SIDE_SHORT
                else f.sector_weights_long
            )
            bucket[sector] = round(bucket.get(sector, 0.0) + weight, 1)

        # Age buckets + drift flag
        try:
            position_history = self._build_position_history(positions)
        except Exception:
            position_history = {}
        for p in positions:
            hist = position_history.get(p.symbol) or {}
            days = hist.get("days_held")
            if days is None:
                continue
            if days < 5:
                f.positions_under_5d += 1
            elif days <= 15:
                f.positions_5_to_15d += 1
            else:
                f.positions_over_15d += 1
            # Drift check — SAME weight and SAME P&L% the PM's own position
            # line renders (`position_weight_pct` / `unrealized_pnl_pct`).
            # This block used to carry raw, un-leveraged weight and a
            # `cost_basis > 0` P&L, so a line reading `Weight: 18.0% DRIFT`
            # sat three lines above `drift-flagged: 0` in one prompt.
            if total_value > 0:
                weight = position_weight_pct(p, total_value)
                pnl_pct = unrealized_pnl_pct(p)
                if _drift_flag_check(weight, pnl_pct):
                    f.positions_drift_flagged += 1

        # Signal freshness
        ages = [a.signal_age_days for a in analyses if a.signal_age_days is not None]
        f.tech_signals_count = len(analyses)
        if ages:
            f.tech_signals_median_age_days = int(statistics.median(ages))
            f.tech_signals_stale_count = sum(1 for a in ages if a >= 8)

        # System perf
        f.rolling_5d_pct = recent_performance.get("rolling_5d_pct")
        f.rolling_20d_pct = recent_performance.get("rolling_20d_pct")

        # RC3: deployment gap vs the invested target as a hard fact in PM's
        # face. The target is the owner's fixed fully-invested mandate
        # (2026-09-17), not a macro output — macro no longer sets or lowers
        # it. Only rendered when there is a book to measure.
        from src.risk.rules import DESK_INVESTED_TARGET_PCT, deployment_gap_band_pct
        if total_value > 0:
            f.invested_target_pct = DESK_INVESTED_TARGET_PCT
            f.deployment_gap_pp = round(
                f.invested_pct - DESK_INVESTED_TARGET_PCT, 1,
            )
            f.deployment_gap_band_pct = deployment_gap_band_pct(
                getattr(self, "config", None)
            )

        # Audit §1.3/§1.4 — the book's real risk, and each position's
        # R-multiple. None on failure; PMFacts.render() then says "unknown"
        # rather than implying the book is risk-free.
        f.heat = self._build_portfolio_heat(positions, total_value)
        # getattr-guarded for the ~58 tests that build TradingPipeline via
        # __new__() without __init__ — same convention as `_sweeper`.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        ceiling = getattr(risk_cfg, "max_portfolio_risk_pct", None)
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            f.risk_ceiling_pct = float(ceiling)
        # Spec §2.2 — render the per-cluster cap the constructor enforces, so
        # the PM sizes a theme against it instead of meeting it as a surprise.
        cluster_share = getattr(risk_cfg, "max_cluster_risk_share_pct", None)
        if isinstance(cluster_share, (int, float)) and 0 < cluster_share <= 100:
            f.cluster_risk_share_pct = float(cluster_share)

        # Audit §1.2 — PM has been told to avoid stacking correlated names
        # while being shown no correlation data at all. Give it the clusters
        # the deterministic check already builds, BEFORE it chooses.
        try:
            from src.data.correlation import correlation_clusters
            universe = {p.symbol for p in positions if p.qty > 0}
            universe |= {a.symbol for a in analyses}
            f.correlation_coverage = bool(correlation_matrix)
            f.correlation_clusters = correlation_clusters(
                universe, correlation_matrix or {},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("pm_facts: correlation clusters failed: %s", e)
            f.correlation_coverage = False
            f.correlation_clusters = []

        # Who these tickers actually are. PM has been reasoning about `CCJ`
        # and `PATH` as price series with a sector tag; "Energy" covers both
        # an integrated major and a pre-revenue nuclear startup, and a sector
        # label alone lets it reach for the wrong prior confidently. Scoped to
        # the symbols already in play for THIS decision (held + candidates) —
        # never the configured universe — and capped, because a cold cache
        # pays one network round trip per symbol and the morning session's
        # budget is not the place to discover a slow yfinance. Every failure
        # mode inside the store degrades to an identity-less profile, which
        # PMFacts.render() drops; this except is the belt to that suspenders.
        try:
            from src.data.company import CompanyProfileStore
            profile_symbols = sorted(
                {p.symbol for p in positions if p.qty > 0}
                | {a.symbol for a in analyses}
            )[:_PM_PROFILE_SYMBOL_CAP]
            f.company_profiles = list(
                CompanyProfileStore()
                .get_many(profile_symbols, allow_fetch=True)
                .values()
            )
        except Exception as e:  # noqa: BLE001 — identity is nice-to-have
            logger.warning("pm_facts: company profiles failed: %s", e)
            f.company_profiles = []

        return f

    def _build_position_facts(self, positions, morning_trades, total_value):
        """Deterministic per-position metrics surfaced to the reviewer.

        Python does the math (progress %, pace, distance-to-stop/target,
        winner flags) so the LLM sees clean numbers and just interprets
        them. Prevents hallucination of percentages.
        """
        # Morning opening-row lookup by symbol for stop/target/days_held.
        # BUY and SHORT are separate: a leftover purchase on the same
        # ticker is not the short's entry.
        buy_rows: dict[str, dict] = {}
        short_rows: dict[str, dict] = {}
        for t in morning_trades or []:
            sym = t.get("symbol")
            act = (t.get("action") or "").upper()
            if not sym or act not in ("BUY", "SHORT"):
                continue
            bucket = short_rows if act == "SHORT" else buy_rows
            if sym not in bucket:
                bucket[sym] = t

        facts: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol
            entry = p.avg_entry
            cur = p.current_price

            # Find the last executed opening row for this symbol to derive
            # target/stop/days_held. A short must read the SHORT row, not a
            # leftover BUY on the same ticker. Falls back to the morning
            # row of the same side, then the matching last-open lookup.
            if p.qty < 0:
                buy = short_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym, action="SHORT")
                    except Exception:
                        buy = None
            else:
                buy = buy_rows.get(sym)
                if not buy:
                    try:
                        buy = self.db.get_symbol_last_buy(sym)
                    except Exception:
                        buy = None

            stop_loss = float((buy or {}).get("stop_loss") or 0)
            # The LIVE target — re-derived if a structural event has since
            # triggered `src.risk.target_revision`. Used for
            # `distance_to_target_pct` and for display, and NEVER as the
            # denominator of progress; see `progress_target` below.
            take_profit = float((buy or {}).get("take_profit") or 0)
            # The ENTRY target, frozen as `initial_take_profit` at insert.
            #
            # THIS, not `take_profit`, is the denominator of
            # `thesis_progress_pct` and therefore of `pace`. The target is
            # the denominator, so RAISING it mechanically LOWERS progress and
            # LOWERS pace — and both are in
            # `src.risk.exit_guard._HIGHER_IS_BETTER`. Measured against the
            # live target, a revision on good news (the name gaps through the
            # resistance the target sat on, the target is re-derived higher)
            # would show up in `MetricDeltas.worsened`, which clears
            # `net_improved`, which switches OFF `veto_contradicted_exit` —
            # so the position would instantly look less progressed and
            # slower than an hour earlier, and a "this position is stalling"
            # SELL that was previously blocked would go through. A machine
            # for making winners look stalled and then selling them.
            #
            # Pinning the denominator removes that by construction rather
            # than by a special case in the guard: a revision cannot move
            # either metric at all, so it cannot appear as a deterioration.
            # Same reasoning as the pinned horizon two paragraphs down — a
            # yardstick that moves measures nothing.
            #
            # Legacy rows that predate the column fall back to the live
            # target, which for them IS the entry target: `take_profit` was
            # only ever written by `insert_trade` before the revision path
            # existed (see the `initial_take_profit` migration in
            # `src/storage/db.py`), and a row with no revision has nothing
            # to diverge from.
            progress_target = float(
                (buy or {}).get("initial_take_profit") or take_profit or 0
            )
            # The ENTRY stop, frozen as `initial_stop_loss` on first
            # write-back. R-multiple's denominator is the bet that was
            # actually made, not the level a trail later ratcheted it to
            # (audit §1.4).
            from src.execution.stop_records import recorded_initial_stop
            initial_stop = recorded_initial_stop(buy)

            # RC1: after any TRAIL_STOP the BUY row's stop is stale-WIDE —
            # the reviewer would see a fat distance_to_stop and keep
            # ratcheting. Prefer live broker truth; fall back to the BUY row.
            try:
                live_stop = self.broker.get_current_stop_price(sym)
            except Exception:  # noqa: BLE001
                live_stop = None
            if isinstance(live_stop, (int, float)) and live_stop > 0:
                stop_loss = float(live_stop)

            # days_held — from BUY timestamp; fall back to None.
            #
            # sessions_held is the holiday-aware companion count (item 165:
            # the broker's real market calendar via `broker.trading_sessions_held`,
            # not the plain Mon-Fri weekday counter in
            # `trading_calendar.trading_sessions_held`, which overstates by
            # one session per market holiday crossed) — the noise-band
            # scaling below needs TRADING SESSIONS, not calendar days, per
            # the 2026-09-04 audit follow-up.
            days_held = None
            sessions_held = None
            buy_ts = (buy or {}).get("timestamp")
            if buy_ts:
                try:
                    from src.trading_calendar import to_et
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(buy_ts.replace("Z", "+00:00")) if "T" in buy_ts \
                        else _dt.strptime(buy_ts, "%Y-%m-%d %H:%M:%S")
                    entry_date = to_et(dt).date()
                    days_held = (et_today() - entry_date).days
                    days_held = max(0, days_held)
                    sessions_held = self.broker.trading_sessions_held(entry_date, et_today())
                except Exception:
                    days_held = None
                    sessions_held = None

            # Phase 3.1 — the thesis horizon and setup type PINNED AT ENTRY.
            # Read from the BUY row, never recomputed. NULL for positions
            # opened before this landed, and for sweep/resume-lane buys with
            # no analysis; those get no pace figure rather than a fabricated
            # one.
            pinned_horizon = (buy or {}).get("expected_horizon_sessions")
            try:
                pinned_horizon = int(pinned_horizon) if pinned_horizon else None
            except (TypeError, ValueError):
                pinned_horizon = None
            setup_type = (buy or {}).get("setup_type") or None
            # Item 82: the MEASURED half of the SAME verdict, pinned at entry
            # alongside `setup_type` (stored 0/1/NULL). Present → this path
            # reaches construction's OWN breakout verdict, not a label-only
            # approximation of it; NULL (legacy row, or a pre-item-82 entry)
            # → `is_trend_trade` falls back to the label alone, exactly as
            # this code did before the column existed. See
            # `src.risk.constants.is_trend_trade`.
            _sc_raw = (buy or {}).get("structural_ceiling")
            structural_ceiling = None if _sc_raw is None else bool(_sc_raw)

            # Progress: 0 at entry, 100 at target, >100 beyond target.
            #
            # DISABLED for breakout ("Type B") setups. A breakout's target is a
            # measured-move reference, not a level anyone is defending — there
            # is no overhead structure for price to progress TOWARD, so
            # "progress" against it measures nothing and "pace" against that
            # nothing is worse. Breakouts are managed by trailing instead
            # (spec Phase 3.7). The breakout verdict is pinned at entry: the
            # analyst's `setup_type` OR the constructor's measured
            # `structural_ceiling` — either sufficient, see `is_trend_trade`.
            # ALSO DISABLED when the whole distance from entry to the target
            # is smaller than one ordinary session's range (2026-09-30).
            #
            # Same defect as the breakout case directly above, reached by a
            # different road. `progress_target - entry` is the denominator,
            # so when the target sits on a wall a fraction of an ATR
            # overhead, `progress_pct` measures the denominator's smallness
            # and not the thesis. META's 2026-09-21 add has $2.00 of room
            # against a $21.22 ATR: a third of one ATR reads as 354%
            # progress, and `target_breach_flag` (>150%) would render
            # "TARGET_BREACH" into the position reviewer's prompt — a seat
            # that can answer SELL or REDUCE. `pace` shares the denominator
            # and sits in `exit_guard._HIGHER_IS_BETTER`, so it moves
            # `MetricDeltas.net_improved` and with it
            # `veto_contradicted_exit`.
            #
            # The owner ruled on 2026-09-30 that a computed target is never
            # an exit trigger. A warning glyph driven off that target is
            # that trigger wearing a different hat, and on a sub-noise
            # denominator it fires on movement that means nothing.
            #
            # This is not a new threshold: it is `MIN_TARGET_ATR_MULTIPLE`,
            # the same noise floor `derive_structural_target` labels the
            # target with, read against the live ATR rather than pinned.
            # Live is correct here and deliberate — the question is whether
            # today's movement can be read as progress, which is a question
            # about today's volatility. It also self-heals the three rows
            # already carrying a pre-fix target.
            from src.risk.constants import is_trend_trade
            from src.data.levels import MIN_TARGET_ATR_MULTIPLE
            live_atr = self._atr_for_symbol(sym)
            target_room_is_noise = bool(
                progress_target and entry
                and live_atr and live_atr > 0
                and abs(progress_target - entry)
                < live_atr * MIN_TARGET_ATR_MULTIPLE
            )
            progress_pct = None
            pace = None
            pace_status = "unavailable"
            if is_trend_trade(setup_type, structural_ceiling=structural_ceiling):
                pace_status = "n/a_breakout"
            elif target_room_is_noise:
                pace_status = "n/a_target_inside_noise"
            else:
                if progress_target and entry and progress_target != entry:
                    progress_pct = (cur - entry) / (progress_target - entry) * 100

                # Pace against the horizon the ANALYST pinned at entry.
                #
                # This replaces `days_held / avg_hold_days`, where avg_hold_days
                # came from the system's own rolling 30-day realized-trade
                # calibration (~2.0 days in practice). That made pace a
                # feedback loop: every early sale shrank the average, which made
                # every surviving position look stalled, which drove more early
                # sales. A self-tightening noose, and the single largest
                # identified P&L defect in the system. A trade's expected
                # horizon must never be derived from the system's own past
                # behaviour.
                #
                # Board item 91: `pinned_horizon` (`expected_horizon_sessions`)
                # is denominated in TRADING SESSIONS, so both sides of this
                # division must be — `sessions_held`, never the calendar-day
                # `days_held`. A weekend adds two calendar days and zero
                # sessions; dividing by calendar days made every position look
                # slower than it is, worst on the short horizons this desk
                # trades, and worse across a holiday weekend. `sessions_held`
                # is the same weekend-aware count the noise-band scaling above
                # already uses (`trading_calendar.trading_sessions_held`) —
                # no new number, just the one already computed above.
                # Board item 165 (owner ruling 2026-09-25): there is NO
                # elapsed-time floor before pace is judged. The old
                # `sessions_held < max(1, pinned_horizon / 3)` "too_early" gate
                # was a made-up clock stacked on a guessed horizon; the desk
                # reassesses every review from the live instrument, so pace is
                # computed and surfaced from the first review whenever the
                # inputs exist. Early pace is naturally extreme (a tiny
                # time_fraction), which the reviewer reads as context — it is
                # never on its own a reason to exit (see the prompt).
                if progress_pct is None or not pinned_horizon or sessions_held is None:
                    pace_status = "unavailable_no_pinned_horizon"
                else:
                    time_fraction = sessions_held / pinned_horizon
                    if time_fraction > 0:
                        pace = progress_pct / (time_fraction * 100)
                        pace_status = "measured"

            # Distance-to-stop / distance-to-target as % of current price.
            #
            # `distance_to_stop_pct` MUST be side-aware. A LONG's stop sits
            # BELOW price, so `(cur - stop_loss)` is the room left and is
            # positive while the position is alive. A SHORT's stop sits
            # ABOVE price, so that same expression is NEGATIVE, and it moves
            # the WRONG way: it gets MORE negative (looks worse under
            # `_HIGHER_IS_BETTER`) as the price falls further from the stop
            # — i.e. as the position gets safer. Mirror the numerator for a
            # short (`p.qty < 0`) so the metric means the same thing on both
            # sides: positive, and falling as the stop gets closer. See
            # `src/risk/exit_guard.py::_HIGHER_IS_BETTER`, which trusts this
            # value to already be direction-corrected.
            dist_stop_pct = None
            dist_target_pct = None
            if stop_loss and cur > 0:
                dist_stop_pct = (
                    (stop_loss - cur) if p.qty < 0 else (cur - stop_loss)
                ) / cur * 100
            if take_profit and cur > 0:
                dist_target_pct = (take_profit - cur) / cur * 100

            # GROSS-leverage weight — the one definition (see
            # `src.risk.rules.weight_pct_of`). Raw here meant the reviewer
            # was shown a 3x fund at a third of the weight the engine caps
            # it at, and the drift flag below never fired on one.
            weight_pct = position_weight_pct(p, total_value)

            # Winner flags. `unrealized_pnl_pct` divides by |entry x qty|;
            # a short's negative qty otherwise flips the sign and feeds the
            # parabolic/drift flags the wrong side. None = unknowable, which
            # is not a flag either way.
            pnl_pct = unrealized_pnl_pct(p)
            parabolic_flag = (
                pnl_pct is not None and pnl_pct >= 15
                and days_held is not None and days_held < 3
            )
            drift_flag = _drift_flag_check(weight_pct, pnl_pct)
            target_breach_flag = progress_pct is not None and progress_pct > 150

            # Vol-unit context so the reviewer reasons about stop distance
            # in ATRs, not raw % (a 3% gap is roomy for KO, suicidal for
            # RKLB). None when bars are unavailable — the prompt treats
            # missing as "unknown", never as zero.
            atr = self._atr_for_symbol(sym)
            atr_pct = round(atr / cur * 100, 2) if (atr and cur > 0) else None
            stop_distance_atrs = None
            if atr and stop_loss and cur > stop_loss:
                stop_distance_atrs = round((cur - stop_loss) / atr, 2)

            # R-multiple (audit §1.4) — profit in units of the risk taken.
            # `thesis_progress_pct` measures distance to TARGET, a different
            # question that does not normalise for how much was risked: a name
            # 20% of the way to a distant target may be +2R or +0.3R, and only
            # the second is a reason to leave it alone.
            from src.risk.metrics import position_risk as _position_risk
            risk = _position_risk(
                symbol=sym, qty=p.qty, entry=entry, current_price=cur,
                stop=stop_loss or None, initial_stop=initial_stop or None,
            )

            facts[sym] = {
                "days_held": days_held,
                "sessions_held": sessions_held,
                "expected_horizon_sessions": pinned_horizon,
                "setup_type": setup_type,
                "pace_status": pace_status,
                "r_multiple": risk.r_multiple,
                "initial_stop": initial_stop or None,
                "risk_released": risk.risk_released,
                "open_risk_dollars": risk.open_risk_dollars,
                "thesis_progress_pct": progress_pct,
                "pace": pace,
                "distance_to_stop_pct": dist_stop_pct,
                "distance_to_target_pct": dist_target_pct,
                # Provenance for `distance_to_stop_pct`, not metrics of
                # their own: that metric is a function of BOTH terms, so
                # without them a rise caused by the stop being widened is
                # indistinguishable from a rise caused by the price moving
                # away. It read as "(improved)" on real 2026-09-01
                # snapshots for V, CMCSA and DIS while all three were
                # deteriorating. See `exit_guard._STOP_DEPENDENT_METRIC`.
                "stop_loss": stop_loss or None,
                "current_price": cur if cur > 0 else None,
                # Side, so the provenance recomputation in
                # `exit_guard.MetricDeltas._distance_move_is_price_driven`
                # can mirror the SAME formula this block uses above
                # (`dist_stop_pct`) rather than assume every position is a
                # long. Never scored — not a metric, just qty's sign.
                "qty": p.qty,
                "weight_pct": weight_pct,
                "parabolic_flag": parabolic_flag,
                "drift_flag": drift_flag,
                "target_breach_flag": target_breach_flag,
                "atr_pct": atr_pct,
                "stop_distance_atrs": stop_distance_atrs,
                # Both targets, named for what they are. `take_profit` is the
                # live (possibly re-derived) number; `entry_take_profit` is
                # the pinned entry derivation that `thesis_progress_pct` and
                # `pace` above are measured against. Surfaced separately so
                # neither the reviewer nor the cockpit has to guess which
                # number a progress figure came from.
                "take_profit": take_profit or None,
                "entry_take_profit": progress_target or None,
                "target_revised": bool(
                    take_profit and progress_target
                    and round(take_profit, 2) != round(progress_target, 2)
                ),
            }
        return facts

    def _build_review_metric_deltas(self, position_facts: dict, *, run_id: str) -> dict:
        """`{symbol: MetricDeltas}` versus this seat's previous review.

        Phase 3.2 / audit §1.5. Degrades to empty deltas (never to a wrong
        comparison) when the prior snapshot is missing or unparseable — a
        first look at a position legitimately has nothing to compare against,
        and the guard downstream treats "no prior" as "do not veto".
        """
        import json as _json
        from src.risk.exit_guard import compute_deltas

        symbols = list(position_facts or {})
        if not symbols:
            return {}
        try:
            prior_rows = self.db.get_prior_position_review_metrics(
                symbols, exclude_run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "review memory: prior-metric read failed (%s) — this review "
                "runs without memory of its own last look", e,
            )
            prior_rows = {}
        deltas: dict = {}
        for symbol, current in (position_facts or {}).items():
            row = prior_rows.get(symbol.upper())
            prior = None
            if row:
                try:
                    prior = _json.loads(row.get("evidence_json") or "{}")
                except (TypeError, ValueError) as e:
                    logger.warning(
                        "review memory: %s prior snapshot is unparseable (%s) — "
                        "treating as no prior", symbol, e,
                    )
                    prior = None
            deltas[symbol.upper()] = compute_deltas(
                symbol, prior, current,
                prior_timestamp=(row or {}).get("timestamp"),
            )
        return deltas

    def _compute_recent_performance(self, current_equity: float) -> dict:
        """Rolling 5-day and 20-day returns from db.daily_pnl, plus the
        §11.2 peak-to-trough drawdown that drives the de-levering ladder.

        Used to tell PM "we have been losing" regardless of what the market
        is doing. Independent of VIX / macro regime (which reflect market,
        not us).

        **The rolling returns are REPORTING ONLY.** They used to carry an
        `in_drawdown` flag, and two thresholds behind it, that halved every  # retired-ok
        new BUY and SHORT. That brake was removed 2026-09-20 on the owner's
        instruction along with the daily-loss halt (retired item 32,
        docs/INCIDENT_HISTORY.md); nothing automatic reads these two numbers
        any more. `peak_to_trough_pct` is a different measure and still
        drives the ladder.

        Returns e.g. {'rolling_5d_pct': -2.3, 'rolling_20d_pct': -6.1,
                      'trailing_days': 18, 'peak_to_trough_pct': -4.4}
        """
        try:
            rows = self.db.get_daily_pnl(limit=25)
        except Exception as e:
            logger.warning("Failed to read daily_pnl for drawdown context: %s", e)
            return {}
        if not rows:
            return {
                "rolling_5d_pct": None, "rolling_20d_pct": None,
                "trailing_days": 0,
                "peak_to_trough_pct": None,
            }

        def _pct_change(start_idx: int) -> float | None:
            if start_idx >= len(rows):
                return None
            start_value = rows[start_idx].get("total_value") or 0
            if start_value <= 0:
                return None
            return round((current_equity - start_value) / start_value * 100, 2)

        # rows are ordered newest-first (DESC); rows[N] = N trading days ago.
        # rows[0] is today, so "5 trading days ago" is rows[5], not rows[4].
        rolling_5d = _pct_change(5)
        rolling_20d = _pct_change(20)

        # Spec §11.2: peak-to-trough drawdown, which drives the de-levering
        # ladder's gross-exposure ceiling. It asks "how far are we off the
        # high-water mark, so how much may the book own". A longer window is
        # read because a high-water mark over 25 sessions is not a
        # high-water mark.
        try:
            hwm_rows = self.db.get_daily_pnl(limit=252)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to read the long daily_pnl window for the §11.2 "
                "high-water mark; falling back to the short one: %s", e,
            )
            hwm_rows = rows
        peak_to_trough = peak_to_trough_pct(
            [r.get("total_value") for r in (hwm_rows or [])], current_equity,
        )

        return {
            "rolling_5d_pct": rolling_5d,
            "rolling_20d_pct": rolling_20d,
            "trailing_days": len(rows),
            "peak_to_trough_pct": peak_to_trough,
        }
