"""Prompt-fact family: names the desk is weighing rather than holding: thesis health of the held book's theses, the missed-opportunities digest and its per-source signals, the watchlist and the blocked proposals.

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210: one of
the six fact families split out of `PromptFacts`. Method bodies are the
former `PromptFacts` bodies byte for byte; this class takes ONLY the
8 collaborators those bodies read. Nothing here may import
`src.pipeline` or `src.pipeline_prompt_facts`.
"""

import json as _json
import re
from src.quantities import avg_dollar_volume, dollar_volumes
from src.risk.metrics import unrealized_pnl_pct
from src.trading_calendar import et_today, session_date_key
from src.prompt_facts_ports import _ABSENT, bind_ports, logger


def _valuation_signal_from(forward_pe: float | None) -> str:
    """Coarse valuation bucket from forward PE. Conservative thresholds:
    anything < 12 is cheap even for growth names; >= 25 is stretched for
    anything that isn't hyper-growth / secular-leader; 12-25 is fair.
    None → no_data (ETFs, newly-listed, yfinance gap). LLM reads this
    AND the raw PE/PS numbers so it can sector-adjust; the enum is the
    fast first cut that prevents obvious hype-chasing on stretched names.
    """
    if forward_pe is None:
        return "no_data"
    try:
        pe = float(forward_pe)
    except (TypeError, ValueError):
        return "no_data"
    if pe <= 0:
        # Negative / zero forward PE → loss-making; can't judge from PE
        # alone. Treat as no_data so the LLM reasons from other signals.
        return "no_data"
    if pe < 12:
        return "cheap"
    if pe >= 25:
        return "stretched"
    return "fair"


def _missed_ops_quality_metrics(
    bars: list, lookback_days: int
) -> tuple[float | None, float | None, float | None]:
    """Compute (avg_dollar_volume_20d_m, volume_confirmation_ratio,
    single_day_concentration_pct) from a list[OHLCV]-like. All three are
    independent — a symbol with only a few bars may return None for
    dollar-volume while still having a valid single-day concentration.

    Designed for the missed_opportunities digest: thin-liquidity top-
    mover symbols (dollar_vol < $5M) and single-day-gap rallies
    (concentration > 70%) shouldn't dominate the evening LLM's attention.

    Returns (None, None, None) when bars is empty or malformed.
    """
    if not bars or len(bars) < 2:
        return None, None, None

    # 20-day dollar volume via the single shared definition
    # (`src.quantities.avg_dollar_volume`) — this digest and the external-
    # symbol admission gate used to compute the same measure two different
    # ways (a halted session was dropped here and counted there, 5.26%
    # apart on a 20-bar window). Only the THRESHOLDS differ now: $5M here,
    # $10M at admission. `min_bars=5` keeps this caller's deliberate
    # tolerance for short history; the gate demands a full window.
    avg_dvol_m: float | None = None
    vol_conf_ratio: float | None = None
    try:
        dollar_vols = dollar_volumes(bars)
        avg_dvol = avg_dollar_volume(bars, min_bars=5)
        if avg_dvol is not None:
            avg_dvol_m = round(avg_dvol / 1_000_000, 2)
            # Today's dollar volume vs the average. >1.5 = buyers showed up.
            if dollar_vols and avg_dvol > 0:
                today_dvol = dollar_vols[-1]
                vol_conf_ratio = round(today_dvol / avg_dvol, 2)
    except (TypeError, ValueError, AttributeError):
        avg_dvol_m = None
        vol_conf_ratio = None

    # Single-day concentration — what fraction of the window's total return
    # came from the biggest single day? > 70% = gap-up day (event/squeeze);
    # < 50% = distributed (trend). Needs ≥ 3 bars in the window to be
    # meaningful (2 bars = one daily return = always 100%).
    window = (bars[-(lookback_days + 1):]
              if len(bars) > lookback_days else bars)
    single_day_conc: float | None = None
    try:
        if len(window) >= 3:
            daily_returns: list[float] = []
            for prev, cur in zip(window[:-1], window[1:]):
                pc_attr = getattr(prev, "close", None)
                cc_attr = getattr(cur, "close", None)
                if not (isinstance(pc_attr, (int, float))
                        and isinstance(cc_attr, (int, float))):
                    continue
                pc = float(pc_attr)
                cc = float(cc_attr)
                if pc > 0:
                    daily_returns.append((cc - pc) / pc * 100.0)
            if daily_returns:
                total = sum(daily_returns)
                max_abs = max((abs(r) for r in daily_returns), default=0.0)
                # Use absolute totals to avoid sign flips when the window
                # has both up and down days.
                if abs(total) > 0.01:
                    # Percentage of the biggest-day move against total
                    # directional move. Cap at 200 — biggest-day move can
                    # exceed total when subsequent days partially reverse.
                    conc = min(max_abs / abs(total) * 100.0, 200.0)
                    single_day_conc = round(conc, 1)
    except (TypeError, ValueError, AttributeError):
        single_day_conc = None

    return avg_dvol_m, vol_conf_ratio, single_day_conc


class CandidateFacts:
    """Names the desk is weighing rather than holding: thesis health of the held book's theses, the missed-opportunities digest and its per-source signals, the watchlist and the blocked proposals."""

    def __init__(
        self,
        *,
        db=_ABSENT,
        broker=_ABSENT,
        market=_ABSENT,
        earnings_provider=_ABSENT,
        news_store=_ABSENT,
        macro_store=_ABSENT,
        config=_ABSENT,
        parse_logged_agent_response=_ABSENT,
    ) -> None:
        bind_ports(self, {
            "db": db,
            "broker": broker,
            "market": market,
            "earnings_provider": earnings_provider,
            "news_store": news_store,
            "macro_store": macro_store,
            "config": config,
            "_parse_logged_agent_response": parse_logged_agent_response,
        })

    def _build_thesis_health_context(
        self,
        positions,
        lookback_weeks: int = 8,
    ) -> dict[str, dict]:
        """Per-position fundamental-evolution snapshot for the evening
        thesis_health_review step.

        For each held symbol, gather:
          - Entry context (date, price, days_held, sessions_held,
            original thesis text) — `sessions_held` is the weekend/holiday-aware
            trading-session count the reviewer reads for pace (item 165)
          - Tech rating trajectory (last 4 ratings as a list)
          - News mentions count + 2 latest headlines (8-week window)
          - Most recent earnings sentiment + key_thesis
          - Current macro sector stance
          - Valuation snapshot (trailing PE / forward PE / P/S / signal)

        Shape designed so the evening LLM can answer
        "strengthening / intact / weakening / broken" per holding,
        not just aggregate-level "bullish / bearish". That step is
        what separates a swing-trader feedback bot from a value-
        investor strategic reflection.

        Returns {symbol: dict}. Empty dict when there are no positions.
        Exceptions during data fetch degrade gracefully — a missing
        field is None or [], the helper does not raise.
        """
        if not positions:
            return {}

        from datetime import timedelta
        lookback_days = lookback_weeks * 7
        tech_map_multi = self._thesis_tech_trajectory_map(lookback_days)
        news_events_map = self._thesis_news_events_map(lookback_days)
        earnings_map = self._missed_ops_earnings_signal()
        macro_map = self._missed_ops_macro_sector_map()

        out: dict[str, dict] = {}
        for p in positions:
            sym = p.symbol

            # Entry context
            entry_date: str | None = None
            entry_reasoning = ""
            days_held: int | None = None
            # Item 165: the reviewer judges PACE ("too slow" → exit early) off
            # holding time, so holding time here must be in TRADING SESSIONS,
            # not calendar days. A weekend or market holiday adds calendar days
            # and zero sessions, so a calendar-day count made a good position
            # look slower than it is and could trigger a real-money early exit.
            # `sessions_held` is the weekend/holiday-aware count the reviewer
            # prompt reads for pace, computed with the SAME helper the
            # morning/midday facts path uses (`broker.trading_sessions_held`,
            # backed by Alpaca's real market calendar) — no parallel method.
            sessions_held: int | None = None
            try:
                buy_row = self.db.get_symbol_last_buy(sym)
            except Exception:
                buy_row = None
            if buy_row:
                ts = (buy_row.get("timestamp") or "")[:10]
                if ts:
                    entry_date = ts
                    try:
                        from datetime import date as _d
                        entry_d = _d.fromisoformat(ts)
                        days_held = max(0, (et_today() - entry_d).days)
                        sessions_held = self.broker.trading_sessions_held(
                            entry_d, et_today(),
                        )
                    except (ValueError, TypeError):
                        days_held = None
                        sessions_held = None
                    except Exception:
                        # Calendar/broker hiccup: degrade this one field to
                        # None rather than dropping the whole thesis-health
                        # row, matching the graceful-degradation contract above.
                        sessions_held = None
                entry_reasoning = (buy_row.get("reasoning") or "")[:300]

            # P&L% — the one definition (`src.risk.metrics.unrealized_pnl_pct`),
            # which returns None when genuinely unknowable. The `cost > 0`
            # guard this replaces silently returned None for EVERY short
            # (a short's `avg_entry * qty` is negative), so evening's
            # thesis-health review saw no P&L on the short book at all.
            _pnl_pct = unrealized_pnl_pct(p)
            pnl_pct = None if _pnl_pct is None else round(_pnl_pct, 2)

            # Tech trajectory — last 4 ratings for this symbol
            tech_trajectory = tech_map_multi.get(sym, [])[:4]

            # News — total count in window + latest 2 headlines
            news_events = news_events_map.get(sym, [])
            news_count = len(news_events)
            latest_news_headlines = [e["event"] for e in news_events[:2]]

            # Sector stance
            sector = ""
            try:
                from src.execution.broker import _get_sector
                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            macro_stance = macro_map.get(sector, "unknown") if sector else "unknown"

            # Valuation — bounded per-symbol yfinance call
            valuation = {
                "trailing_pe": None, "forward_pe": None, "ps_ratio": None,
            }
            try:
                v = self.market.get_valuation_metrics(sym) or {}
                valuation["trailing_pe"] = v.get("trailing_pe")
                valuation["forward_pe"] = v.get("forward_pe")
                valuation["ps_ratio"] = v.get("ps_ratio")
            except Exception:
                pass
            valuation["signal"] = _valuation_signal_from(valuation["forward_pe"])

            # Earnings deep-dive: full reasoning_chain + headline metrics
            # from the canonical analysis_*.md for this symbol. Only
            # surfaced for HELD positions (token-budget reasons); missed_ops
            # still use the 140-char snippet via earnings_map.
            from src.data.earnings_deep_dive import load_earnings_deep_dive
            deep_dive = None
            try:
                manifest = getattr(self.earnings_provider, "manifest", {}) or {}
                deep_dive = load_earnings_deep_dive(sym, manifest)
            except Exception as exc:
                logger.debug(
                    "thesis_health earnings deep-dive failed for %s: %s",
                    sym, exc,
                )

            out[sym] = {
                "symbol": sym,
                "entry_date": entry_date,
                "entry_reasoning": entry_reasoning,
                "days_held": days_held,
                "sessions_held": sessions_held,
                "entry_price": p.avg_entry,
                "current_price": p.current_price,
                "pnl_pct": pnl_pct,
                "sector": sector,
                "tech_trajectory": tech_trajectory,
                "news_count_8w": news_count,
                "latest_news_headlines": latest_news_headlines,
                "recent_earnings_signal": earnings_map.get(sym),
                "earnings_deep_dive": deep_dive,
                "macro_sector_stance": macro_stance,
                "valuation": valuation,
            }
        return out

    def _thesis_tech_trajectory_map(
        self, lookback_days: int,
    ) -> dict[str, list[str]]:
        """For each symbol, extract chronological tech ratings from the last
        `lookback_days` of tech_analyst logs. Returns {sym: ["buy","hold",
        "buy","strong_buy"]} newest-first. Uses the same shape-normalizer
        as the missed_ops digest so bare-list / dict-wrapped / symbol-keyed
        shapes all work. Empty dict on failure."""
        from src.evolution.quarterly_digest import _tech_analyses_from_data
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst",
                limit=lookback_days,
                before_date=None,
            )
        except Exception as exc:
            logger.warning("thesis_tech_trajectory: logs fetch failed: %s", exc)
            return {}
        by_sym: dict[str, list[str]] = {}
        for row in rows:
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if sym and rating:
                    by_sym.setdefault(sym, []).append(str(rating))
        return by_sym

    def _thesis_news_events_map(
        self, lookback_days: int,
    ) -> dict[str, list[dict]]:
        """Per-symbol news events over the lookback window. Returns
        {sym: [{event, conviction, date}, ...]} newest-first.

        Walks dated full_report.json files. Every state_change with the
        symbol in affected_symbols is collected. Wider than the 5-day
        window _missed_ops_news_signal uses because the thesis health
        review needs to see the full 8-week arc, not just recent days.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, list[dict]] = {}
        today = et_today()
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                affected = ch.get("affected_symbols", []) or []
                conviction = (ch.get("conviction") or "").lower()
                for sym in affected:
                    sym_u = str(sym).upper()
                    if not sym_u:
                        continue
                    out.setdefault(sym_u, []).append({
                        "event": event[:140],
                        "conviction": conviction,
                        "date": str(day),
                    })
        return out

    def _build_watchlist_candidates(
        self, lookback_days: int = 30,
    ) -> list[dict]:
        """Symbols the evening analyst has repeatedly flagged as "add" or
        "watch" to the trading universe — the surface the user reviews
        when deciding whether to actually expand the 77-symbol universe.

        Reads `insights.missed_opportunities_json` for the last N days,
        filters entries with `universe_addition_recommendation != "no"`,
        aggregates by symbol.

        Returns a sorted list of dicts:
          [
            {
              "symbol": "VST",
              "add_count": int,
              "watch_count": int,
              "total_flags": int,
              "dates": [ISO date, ...],   # newest first
              "themes": [str, ...],        # distinct theme_if_any seen
              "latest_reason": str,        # most recent universe_addition_reason
              "latest_miss_category": str, # e.g. "theme_blindspot"
            },
            ...
          ]

        Sort: (add_count desc, watch_count desc, total_flags desc, symbol).
        One "add" carries more weight than one "watch" — an "add" means
        the LLM cleared ALL four quality bars (volume + sustain + theme
        + fundamentals), a "watch" means most-but-not-all.

        THIS FUNCTION DOES NOT MODIFY THE UNIVERSE. Universe expansion
        is a human decision — edit config/settings.yaml manually after
        reviewing this output. By design, so that the system can't
        casually grow the curated list.

        The aggregation itself is a pure function
        (`src.watchlist_candidates.build_watchlist_candidates`) — this
        method is now a thin fetch-then-aggregate wrapper so
        `src/api/db_reads.py` can compute the identical output from its own
        read-only `insights` query without importing `TradingPipeline`
        (Stage 2 Checkpoint C).
        """
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as exc:
            logger.warning(
                "watchlist_candidates: insights fetch failed: %s", exc,
            )
            return []
        if not rows:
            return []
        from src.watchlist_candidates import build_watchlist_candidates
        return build_watchlist_candidates(rows, lookback_days)

    def _build_blocked_proposals(
        self,
        lookback_days: int = 21,
        min_proposals: int = 3,
        max_lines: int = 5,
    ) -> str:
        """PM memory: names it keeps asking for and never gets, and why.

        Every other per-symbol memory PM reads (loss pits, missed lessons,
        position history, R-multiples) is keyed on a POSITION, so a symbol
        that never became a position is invisible to all of them — however
        many times PM proposed it. This is the only section that can see a
        block, and a block is the cleanest feedback the desk produces: it
        arrives with its cause attached, where a filled trade's loss is
        confounded by whatever the market did next.

        Computed at prompt-build time from existing tables — no schema
        change. `specialist_evidence` marks each stage of a proposal's life
        and `decision_id` joins it to `trades`:

            target → proposed_order → verdict → execution_skip | trades.fill

        A `target` is one proposal. Targets sized to zero are EXIT
        instructions, not requests to get in, so they are excluded — a
        blocked exit is a different defect and counting it here would
        overstate the entry-side block rate.

        Every blocking reason is copied VERBATIM out of stored data —
        `execution_skip.reason` (`qty_zero`, `geometry_unmeasurable`,
        historical `geometry_rr`, `insufficient_cash`), `verdict.reason_category` (`rr_fail`, …),
        `trades.fill_status` (`canceled`, …) — so this section and the
        RM-verdict section name the same failure the same way. Exactly three
        tokens are ours: `rm_zeroed`, `order_not_placed` and
        `no_order_built`. Each describes an ABSENCE, which no table records:
        nothing was written, so nothing can be quoted. They are kept
        distinct because "the order was never built" and "the order was
        built and never placed" are different halves of the machinery.

        Conversion is judged on any `filled` trade sharing the proposal's
        `decision_id`. Today only entry orders carry a `decision_id`, so
        that is exact; if exits ever carry one, this biases toward calling a
        proposal converted, which makes the section quieter rather than
        making it cry wolf.

        Diagnostic only. Nothing here gates, filters or caps anything.
        That is final, not interim: a count-based re-proposal gate was
        ANSWERED NO on 2026-09-14 (docs/WORK.md item 10(b)) because the
        conversion rate measures this desk's own gates and plumbing, not
        the instrument. Do not add one.

        Returns "" when the window holds no proposals at all — PM's section
        then shows its own "no proposals on record" default. When there are
        proposals but no repeat offender, the aggregate line still renders
        with an explicit "none" so the desk can never mistake a quiet
        section for a missing one.
        """
        import json as _json
        from datetime import timedelta
        try:
            since = (et_today() - timedelta(days=lookback_days)).isoformat()
            raw = self.db.get_proposal_funnel_rows(since)
        except Exception as e:
            logger.warning("blocked_proposals: DB fetch failed: %s", e)
            return ""

        proposals: list[tuple[str, str, str]] = []   # (ts, decision_id, symbol)
        ordered: set[tuple[str, str]] = set()        # (decision_id, symbol)
        skips: dict[tuple[str, str], str] = {}       # → verbatim reason
        verdicts: dict[str, dict] = {}               # decision_id → verdict
        constructor_drops: dict[tuple[str, str], str] = {}  # → constructor's own reason
        data_faults: dict[tuple[str, str], str] = {}        # → FAULT_* code (unmeasurable)
        constructor_refusals: dict[tuple[str, str], str] = {}  # → "constructor_refused:<code>"
        for row in raw.get("evidence") or []:
            kind = row.get("kind")
            did = row.get("decision_id")
            if not did:
                continue
            try:
                data = _json.loads(row.get("evidence_json") or "{}")
            except (TypeError, ValueError) as e:
                # One unparseable row must not blank the whole section, but a
                # silent drop hides a proposal PM did make. Same discipline as
                # the L3d/L3f builders above.
                logger.warning(
                    "blocked_proposals: JSON parse failed for %s row %s: %s",
                    kind, (row.get("timestamp") or "?"), e,
                )
                continue
            if not isinstance(data, dict):
                continue
            if kind == "verdict":
                verdicts[did] = data
                continue
            sym = (row.get("symbol") or data.get("symbol") or "").strip().upper()
            if not sym:
                continue
            if kind == "target":
                # `risk_allocation_pct` is the live field; `target_weight_pct`
                # is the legacy one older rows carry. Either can size a
                # target (see TargetPosition), so read whichever is present.
                size = data.get("risk_allocation_pct")
                if size is None:
                    size = data.get("target_weight_pct")
                try:
                    if size is None or float(size) <= 0.0:
                        continue        # an exit instruction, not a proposal
                except (TypeError, ValueError):
                    continue
                proposals.append((row.get("timestamp") or "", did, sym))
            elif kind == "proposed_order":
                ordered.add((did, sym))
            elif kind == "execution_skip":
                reason = (data.get("reason") or "").strip()
                if reason:
                    skips[(did, sym)] = reason
            elif kind == "pipeline_event":
                # The deterministic constructor's own reason for dropping a
                # target before it ever became a `proposed_order` row (see
                # `pipeline_stages.DecisionStage`, which persists this via
                # `PortfolioConstructor.last_drop_reasons`). Mirrors
                # `scripts/blocked_proposals_census.py::_load_recorded_reasons`
                # (renamed 2026-09-11 when that script started reading three
                # more durable reason kinds the same way — symbol_guard,
                # hard_risk, risk_manager_unparseable_output — this PM-facing
                # helper does NOT read those three yet, only
                # `constructor_dropped`)
                # — without it, a constructor drop falls through to the
                # generic `no_order_built` bucket below with no explanation,
                # even though the real reason was captured at drop time.
                if (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "blocked"
                        and data.get("reason") == "constructor_dropped"):
                    constructor_drops[(did, sym)] = (
                        data.get("detail") or "constructor_dropped"
                    )
                elif (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "unmeasurable"
                        and data.get("reason") == "data_fault"):
                    # 2026-09-12: a symbol the constructor could not
                    # MEASURE (no price / ATR / usable bars / analysis).
                    # Its own bucket — not `constructor_dropped`, which is
                    # for trades the desk judged. Mirrors
                    # `scripts/blocked_proposals_census.py`.
                    data_faults[(did, sym)] = str(data.get("fault") or "unknown")
                # 2026-09-12: a refusal the constructor recorded AS DATA
                # (`PortfolioConstructor.last_refusals`, filed by
                # `DecisionStage` under `constructor_refused` with the code
                # beside it — today `no_structural_stop_and_no_
                # volatility_reading`; the young-listing bar-count refusal
                # was dropped, item 180, and the stop-WIDTH refusal
                # `stop_wider_than_instrument_reach` was deleted 2026-09-26,
                # item 56, after refusing nothing in 648 sized stops).
                # Kept apart from
                # the regex-recovered `constructor_dropped` so the digest
                # names the rule, not a sentence.
                elif (data.get("stage") == "deterministic_gate"
                        and data.get("outcome") == "blocked"
                        and data.get("reason") == "constructor_refused"):
                    constructor_refusals[(did, sym)] = (
                        f"constructor_refused:{data.get('refusal') or 'unknown'}"
                    )

        if not proposals:
            return ""

        fills: dict[tuple[str, str], str] = {}
        for row in raw.get("trades") or []:
            did = row.get("decision_id")
            sym = (row.get("symbol") or "").strip().upper()
            if not did or not sym:
                continue
            status = (row.get("fill_status") or "").strip().lower()
            if not status:
                continue
            # A decision can emit more than one order for a symbol (a retry, a
            # repeg). One fill converts the proposal, so a filled row wins
            # over any other status regardless of arrival order.
            if fills.get((did, sym)) == "filled":
                continue
            fills[(did, sym)] = status

        def _outcome(did: str, sym: str) -> str | None:
            """None == converted. Otherwise the verbatim blocking reason."""
            key = (did, sym)
            status = fills.get(key)
            if status == "filled":
                return None
            if status:
                return f"order_{status}"
            if key in skips:
                return skips[key]
            if key in data_faults:
                # Not a trade judgement: the desk could not measure the
                # symbol. Named by fault so a feed outage and a missing
                # analysis stay distinguishable in the digest.
                return f"data_fault:{data_faults[key]}"
            if key in constructor_refusals:
                # Same precedence as a constructor drop (the constructor
                # runs before the Risk Manager), but the CODE is the
                # category, so "no floor" aggregates under its own line.
                return constructor_refusals[key]
            if key in constructor_drops:
                # Checked before the verdict/`ordered` logic below, so a
                # symbol the deterministic constructor dropped before the
                # Risk Manager ever saw the plan is attributed to the
                # constructor, never to the RM's veto of whatever plan
                # survived. A fixed category (not the per-symbol detail
                # text) so this still aggregates in `top` below; the real
                # sentence lives in `constructor_drops[key]` for anyone
                # who wants it. Mirrors
                # `scripts/blocked_proposals_census.py::classify`.
                return "constructor_dropped"
            # A verdict rejection/zeroing is only attributed to a symbol
            # confirmed to have reached the constructor's own order list
            # (`ordered`). Without this guard every ORIGINALLY-proposed
            # symbol gets blamed for an AI Risk Manager veto — including
            # ones the deterministic constructor had already dropped
            # before the Risk Manager ever saw the plan. Mirrors
            # `scripts/blocked_proposals_census.py::classify`.
            verdict = verdicts.get(did)
            if isinstance(verdict, dict) and key in ordered:
                if verdict.get("approved") is False:
                    cat = (verdict.get("reason_category") or "").strip()
                    return f"rm_rejected:{cat}" if cat else "rm_rejected"
                for mod in (verdict.get("modifications") or []):
                    if not isinstance(mod, dict):
                        continue
                    if (mod.get("symbol") or "").strip().upper() != sym:
                        continue
                    try:
                        if float(mod.get("new_value")) == 0.0:
                            return "rm_zeroed"
                    except (TypeError, ValueError):
                        continue
            if key in ordered:
                return "order_not_placed"
            return "no_order_built"

        by_symbol: dict[str, list[tuple[str, str | None]]] = {}
        block_counts: dict[str, int] = {}
        converted = 0
        for ts, did, sym in proposals:
            reason = _outcome(did, sym)
            by_symbol.setdefault(sym, []).append((ts, reason))
            if reason is None:
                converted += 1
            else:
                block_counts[reason] = block_counts.get(reason, 0) + 1

        total = len(proposals)
        pct = (100.0 * converted / total) if total else 0.0
        top = sorted(block_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        lines = [
            f"Conversion: {converted} of {total} proposals reached a fill "
            f"({pct:.0f}%) in the last {lookback_days} days.",
        ]
        if top:
            lines.append(
                "Top blocks: "
                + ", ".join(f"{reason} × {n}" for reason, n in top)
                + "."
            )

        repeats = [
            (sym, rows) for sym, rows in by_symbol.items()
            if len(rows) >= min_proposals
            and all(reason is not None for _, reason in rows)
        ]
        if not repeats:
            lines.append(
                f"Repeat blocked names: none — no symbol was proposed "
                f"{min_proposals}+ times without a fill in this window."
            )
            return "\n".join(lines)

        repeats.sort(key=lambda item: (-len(item[1]), item[0]))
        lines.append(
            f"Repeat blocked names ({min_proposals}+ proposals, 0 fills):"
        )
        for sym, rows in repeats[:max_lines]:
            rows = sorted(rows, key=lambda r: r[0], reverse=True)  # newest first
            sessions = len({ts[:10] for ts, _ in rows if ts})
            recent = ", ".join(str(reason) for _, reason in rows[:3])
            lines.append(
                f"- {sym}: proposed {len(rows)}× across {sessions} sessions, "
                f"filled 0 — most recent first: {recent}"
            )
        return "\n".join(lines)

    def _build_missed_opportunities_digest(
        self,
        lookback_days: int = 5,
        move_threshold_pct: float = 8.0,
        top_n: int = 15,
        top_movers_count: int = 15,
        current_position_symbols: set[str] | None = None,
        min_top_mover_dollar_volume_m: float = 5.0,
    ) -> list:
        """Notable movers we did NOT own — input for evening's missed-op review.

        Symbol set = trading universe ∪ Alpaca top gainers. For each, compute
        the `lookback_days` window return; keep those crossing
        `move_threshold_pct` (absolute). Tag each with the signal state that
        was visible at the time (prior TA rating, news headline, earnings
        sentiment, macro sector stance) so the LLM's miss classification has
        to cite observable evidence, not retro-rationalize price.

        Quality filter for TOP-MOVER symbols only (universe symbols always
        pass — they're curated): if 20-day avg dollar volume is below
        `min_top_mover_dollar_volume_m` (default $5M), the symbol is
        dropped before reaching the LLM. Thin-liquidity gappers aren't
        interesting to a medium-long-term investor and flooding the prompt
        with them dilutes the real misses.

        Returns a list[MissedOpportunitySnapshot]. Empty when no symbol
        crosses the threshold. Sort order within the list:
          (a) not-held, has prior signal — real "we saw it, didn't act" misses
          (b) not-held, no prior signal — theme-coverage blindspots
          (c) already held — context for decision-quality review
        Within each group by |move_pct| descending. Top `top_n` only.
        """
        from src.models import MissedOpportunitySnapshot

        universe = list(getattr(self.config.trading, "universe", []) or [])
        universe_set = {s.upper() for s in universe if s}
        try:
            top_movers = self.broker.get_top_movers(n=top_movers_count) or []
        except Exception as exc:
            logger.warning("missed_ops: get_top_movers failed: %s", exc)
            top_movers = []
        top_mover_syms = {
            str(m["symbol"]).upper() for m in top_movers
            if isinstance(m, dict) and m.get("symbol")
        }
        all_syms = universe_set | top_mover_syms
        if not all_syms:
            return []

        # Fetch bars once per symbol. Cache for reuse across move + quality
        # metric computation. Need ≥ 25 bars for a 20-day average volume
        # calculation, so we pad to that even if lookback_days is tight.
        bars_pad = max(lookback_days + 3, 25)
        bars_cache: dict[str, list] = {}
        for sym in all_syms:
            try:
                bars = self.market.get_ohlcv(sym, lookback_days=bars_pad)
            except Exception:
                continue
            if bars and len(bars) >= 2:
                bars_cache[sym] = bars

        # Per-symbol window return.
        symbol_moves: dict[str, float] = {}
        for sym, bars in bars_cache.items():
            window = bars[-(lookback_days + 1):] if len(bars) > lookback_days else bars
            if len(window) < 2:
                continue
            start_close = getattr(window[0], "close", 0) or 0
            end_close = getattr(window[-1], "close", 0) or 0
            if start_close <= 0:
                continue
            move_pct = (end_close - start_close) / start_close * 100.0
            symbol_moves[sym] = round(move_pct, 2)

        candidates = {
            s: m for s, m in symbol_moves.items()
            if abs(m) >= move_threshold_pct
        }
        if not candidates:
            return []

        # Pre-compute signal maps once (not per-symbol): cheap vs. re-running
        # DB/file scans inside the loop.
        held_set = self._missed_ops_held_set(
            lookback_days, current_position_symbols or set()
        )
        tech_map = self._missed_ops_tech_signal(lookback_days)
        news_map = self._missed_ops_news_signal(lookback_days)
        theme_map = self._missed_ops_theme_tags(lookback_days)
        earnings_map = self._missed_ops_earnings_signal()
        macro_sector_map = self._missed_ops_macro_sector_map()

        snapshots: list = []
        for sym, move_pct in candidates.items():
            if sym in universe_set and sym in top_mover_syms:
                source = "both"
            elif sym in top_mover_syms:
                source = "top_mover"
            else:
                source = "universe"

            bars = bars_cache.get(sym) or []
            avg_dvol_m, vol_conf_ratio, single_day_conc = _missed_ops_quality_metrics(
                bars, lookback_days,
            )

            # Liquidity pre-filter: thin TOP-MOVER-only symbols drop out here.
            # Universe symbols bypass — they're already curated for quality.
            if (source == "top_mover"
                    and avg_dvol_m is not None
                    and avg_dvol_m < min_top_mover_dollar_volume_m):
                logger.debug(
                    "missed_ops: dropping thin top-mover %s (avg $vol %.1fM < %.1fM)",
                    sym, avg_dvol_m, min_top_mover_dollar_volume_m,
                )
                continue

            ta_rating, ta_date = tech_map.get(sym, (None, None))
            had_ta = ta_rating in ("buy", "strong_buy")
            news_headline = news_map.get(sym)
            earnings_signal = earnings_map.get(sym)

            sector_stance = "unknown"
            try:
                from src.execution.broker import _get_sector
                sector = _get_sector(sym) or ""
            except Exception:
                sector = ""
            if sector and sector in macro_sector_map:
                sector_stance = macro_sector_map[sector]

            # Valuation (done per-candidate after threshold filter → only
            # ~5-15 yfinance calls, not 90+). Defaults to all-None on
            # error / ETF / data gap.
            trailing_pe = None
            forward_pe = None
            ps_ratio = None
            try:
                val_info = self.market.get_valuation_metrics(sym) or {}
                trailing_pe = val_info.get("trailing_pe")
                forward_pe = val_info.get("forward_pe")
                ps_ratio = val_info.get("ps_ratio")
            except Exception as exc:
                logger.debug(
                    "missed_ops valuation fetch failed for %s: %s", sym, exc,
                )
            valuation_signal = _valuation_signal_from(forward_pe)

            # Bidirectional opportunity framing: a DOWN move with an
            # intact fundamental signal is the classic value-dip the
            # medium-long-term investor wants to catch. Flag it at the
            # snapshot level so the evening LLM's value_entry_missed
            # classification is grounded, not just vibes.
            has_fundamental_signal = (
                news_headline is not None or earnings_signal is not None
            )
            value_entry_candidate = (
                move_pct <= -8.0 and has_fundamental_signal
            )

            snapshots.append(MissedOpportunitySnapshot(
                symbol=sym,
                move_pct=move_pct,
                window_days=lookback_days,
                held_during_window=(sym in held_set),
                had_ta_signal=had_ta,
                had_news_signal=(news_headline is not None),
                had_earnings_signal=(earnings_signal is not None),
                source=source,
                last_ta_rating=ta_rating,
                last_ta_date=ta_date,
                last_news_headline=news_headline,
                theme_tags=theme_map.get(sym, [])[:4],
                recent_earnings_signal=earnings_signal,
                macro_sector_tailwind=sector_stance,  # type: ignore[arg-type]
                avg_dollar_volume_20d_m=avg_dvol_m,
                volume_confirmation_ratio=vol_conf_ratio,
                single_day_concentration_pct=single_day_conc,
                trailing_pe=trailing_pe,
                forward_pe=forward_pe,
                ps_ratio=ps_ratio,
                valuation_signal=valuation_signal,  # type: ignore[arg-type]
                value_entry_candidate=value_entry_candidate,
            ))

        # Drop names we actually held during the window before sorting and
        # truncating. The prompt instructs the LLM to recognize HELD rows
        # and not classify them as "missed", but the LLM-only fence is
        # fragile: a hiccup could let evening emit `value_entry_missed`
        # on a name we literally bought today (held_during_window=True).
        # Pre-filter in Python so even a confused LLM can't surface a
        # held name. Held positions still get full coverage via
        # thesis_health_review — they don't need a "missed" entry.
        snapshots = [s for s in snapshots if not s.held_during_window]

        def _priority_key(s) -> tuple:
            any_signal = s.had_ta_signal or s.had_news_signal or s.had_earnings_signal
            group = 0 if any_signal else 1
            return (group, -abs(s.move_pct))

        snapshots.sort(key=_priority_key)
        return snapshots[:top_n]

    def _missed_ops_held_set(
        self, lookback_days: int, current_position_symbols: set[str]
    ) -> set[str]:
        """Symbols we owned (or traded) within the window.

        Union of (a) symbols currently open in ctx.positions and (b) symbols
        with any executed trade in the last ~2×`lookback_days` calendar days
        (accounts for weekends / holidays). Over-inclusive on purpose — better
        to NOT flag a legitimate hold as "missed" than invent a miss from a
        stale SELL earlier in the week.
        """
        from datetime import timedelta
        held: set[str] = {s.upper() for s in current_position_symbols if s}
        try:
            rows = self.db.get_trades(limit=500, executed_only=True)
        except Exception as exc:
            logger.warning("missed_ops: get_trades failed: %s", exc)
            return held
        cutoff = et_today() - timedelta(days=lookback_days * 2 + 2)
        cutoff_str = cutoff.isoformat()
        for r in rows:
            ts_date = (r.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            sym = (r.get("symbol") or "").upper()
            if sym:
                held.add(sym)
        return held

    def _missed_ops_tech_signal(
        self, lookback_days: int
    ) -> dict[str, tuple[str, str]]:
        """Most recent TA rating per symbol in window → {symbol: (rating, date)}.

        Walks recent tech_analyst agent_logs, parses the batch-output JSON,
        takes the newest rating per symbol. `rating in ("buy","strong_buy")`
        is what drives the `had_ta_signal` flag downstream.

        Production tech_analyst emits two different JSON shapes depending on
        which code path wrote the log — either ``{"analyses": [...]}`` or a
        BARE LIST of per-symbol dicts. We delegate shape normalization to
        `quarterly_digest._tech_analyses_from_data` so both paths stay in
        sync — adding a third shape should only require editing that helper.
        """
        from datetime import timedelta
        from src.evolution.quarterly_digest import _tech_analyses_from_data
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="tech_analyst", limit=lookback_days * 3,
                before_date=None,
            )
        except Exception as exc:
            logger.warning("missed_ops: tech_analyst logs fetch failed: %s", exc)
            return {}
        cutoff_str = (et_today() - timedelta(days=lookback_days * 2 + 2)).isoformat()
        latest: dict[str, tuple[str, str]] = {}
        for row in rows:
            ts_date = (row.get("timestamp") or "")[:10]
            if not ts_date or ts_date < cutoff_str:
                continue
            data = self._parse_logged_agent_response(row)
            if data is None:
                continue
            for a in _tech_analyses_from_data(data):
                sym = (a.get("symbol") or "").upper()
                rating = a.get("rating")
                if not sym or not rating:
                    continue
                if sym not in latest:  # newer rows first from get_recent_agent_outputs
                    latest[sym] = (str(rating), ts_date)
        return latest

    def _missed_ops_news_signal(self, lookback_days: int) -> dict[str, str]:
        """Most recent news headline touching each symbol in window.

        Walks dated full_report.json files. For state_changes, harvests
        (event-text, affected_symbols) pairs. For stock_news, takes the first
        alert's headline. Newest day wins. Headlines clipped to 140 chars so
        they don't blow the prompt budget.
        """
        import json as _json
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, str] = {}
        today = et_today()
        # Iterate newest → oldest so first-seen wins (freshest headline per symbol).
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                if not event:
                    continue
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if sym_u and sym_u not in out:
                        out[sym_u] = event[:140]
            for sym, items in (report.get("stock_news") or {}).items():
                sym_u = str(sym).upper()
                if sym_u in out or not items:
                    continue
                first = items[0] if isinstance(items, list) else None
                if isinstance(first, dict):
                    headline = (first.get("headline") or "").strip()
                    if headline:
                        out[sym_u] = headline[:140]
        return out

    def _missed_ops_theme_tags(self, lookback_days: int) -> dict[str, list[str]]:
        """Rough theme proxies per symbol from recent state_change event text.

        Extracts the first 1-2 meaningful tokens from each event and tags the
        affected symbols with them. Not a semantic classifier — the LLM
        refines to one canonical theme name in `MissedOpportunity.theme_if_any`.
        Purpose here is surface pattern co-occurrence ("AVGO: ai-capex, compute")
        so the LLM can spot the theme instead of treating each headline
        in isolation.
        """
        import json as _json
        import re
        from datetime import timedelta
        from pathlib import Path
        news_dir = getattr(self.news_store, "data_dir", None)
        if news_dir is None:
            return {}
        out: dict[str, list[str]] = {}
        stopwords = {
            "this", "that", "with", "from", "into", "than", "will", "would",
            "should", "could", "about", "against", "between", "report",
        }
        today = et_today()
        for days_ago in range(lookback_days + 1):
            day = today - timedelta(days=days_ago)
            report_path = Path(news_dir) / str(day) / "full_report.json"
            if not report_path.exists():
                continue
            try:
                report = _json.loads(report_path.read_text())
            except (_json.JSONDecodeError, OSError):
                continue
            for ch in report.get("state_changes", []) or []:
                event = (ch.get("event") or "").strip()
                tokens = [
                    t.lower() for t in re.findall(r"[A-Za-z]{4,}", event)
                    if t.lower() not in stopwords
                ]
                if not tokens:
                    continue
                tag = "-".join(tokens[:2])
                for sym in ch.get("affected_symbols", []) or []:
                    sym_u = str(sym).upper()
                    if not sym_u:
                        continue
                    bucket = out.setdefault(sym_u, [])
                    if tag not in bucket and len(bucket) < 4:
                        bucket.append(tag)
        return out

    def _missed_ops_earnings_signal(self) -> dict[str, str]:
        """Most recent non-bearish earnings take per symbol from on-disk cache.

        Walks earnings_provider.manifest, skips abandoned entries, reads each
        analysis file's head (first 600 chars) and passes any entry whose
        head text contains no "bearish" token. Returns {symbol: snippet} where
        snippet is a clipped first-sentence-ish summary the LLM can cite as
        evidence for `fundamentals_mispricing` classification.
        """
        try:
            manifest = getattr(self.earnings_provider, "manifest", {}) or {}
        except Exception:
            return {}
        from datetime import date as _date
        from pathlib import Path

        # audit round 2, three fixes:
        # (a) newest filing PER SYMBOL — the old loop wrote raw manifest
        #     order, so an older 10-K could shadow this quarter's 10-Q;
        # (b) 90-day recency using the manifest's own filing_date — a stale
        #     analysis from months ago is not "recent earnings evidence";
        # (c) sentiment from the STRUCTURED "Sentiment:" line — the naive
        #     `"bearish" in head` substring dropped NEUTRAL analyses whose
        #     prose merely mentioned the word ("not bearish", "bearish
        #     scenarios considered").
        best: dict[str, tuple[str, dict]] = {}   # symbol -> (filing_date, entry)
        for key, entry in manifest.items():
            if not isinstance(entry, dict) or entry.get("abandoned"):
                continue
            symbol = str(key).split("_")[0].upper()
            fd = str(entry.get("filing_date") or "")
            if symbol not in best or fd > best[symbol][0]:
                best[symbol] = (fd, entry)

        out: dict[str, str] = {}
        today = et_today()
        for symbol, (fd, entry) in best.items():
            try:
                if not fd or (today - _date.fromisoformat(fd)).days > 90:
                    continue
            except ValueError:
                continue   # unparseable date = unknowable age = stale
            analysis_path = entry.get("analysis_path")
            if not analysis_path:
                continue
            p = Path(analysis_path)
            if not p.exists():
                continue
            try:
                text = p.read_text()
            except OSError:
                continue
            head = text[:600]
            m = re.search(r"^\s*-?\s*\*{0,2}Sentiment\*{0,2}\s*:\s*(\w+)",
                          head, re.MULTILINE | re.IGNORECASE)
            sentiment = (m.group(1).lower() if m else None)
            if sentiment == "bearish":
                continue
            if sentiment is None and "bearish" in head.lower():
                continue   # no structured line — keep the conservative fallback
            snippet = head.replace("\n", " ").strip()[:140]
            if snippet:
                out[symbol] = snippet
        return out

    def _missed_ops_macro_sector_map(self) -> dict[str, str]:
        """Latest macro sector stance: {sector: bullish|neutral|bearish}.

        Reads macro_store.load_last_state() — persisted at the end of each
        morning macro run. Missing keys / stances → empty dict, snapshot
        defaults to "unknown" for each symbol, which is itself a signal (if
        macro never covers a whole sector we rally through, that's a
        coverage blindspot the quarterly meta-reflector should notice).
        """
        try:
            state = self.macro_store.load_last_state() or {}
        except Exception as exc:
            logger.warning("missed_ops: macro_store load failed: %s", exc)
            return {}
        guidance = state.get("sector_guidance") or {}
        if not isinstance(guidance, dict):
            return {}
        out: dict[str, str] = {}
        for sector, stance in guidance.items():
            if (isinstance(stance, str)
                    and stance in ("bullish", "neutral", "bearish")):
                out[str(sector)] = stance
        return out
