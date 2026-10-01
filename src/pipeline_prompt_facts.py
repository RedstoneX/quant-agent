"""Prompt-facts builders: read-only DB/broker reads turned into LLM context.

Step 1 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

The defining property of this module: it places no orders, cancels nothing and
amends no stop. `_handle_ex_dividends` sat in this cluster's line range and does
move live stops, so it is NOT here — it goes to the protection module in step 2
(plan §1 correction, 2026-10-01).

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json
import logging
import re
from pathlib import Path

from src.models import TechAnalysisResult
from src.pipeline_context import PMFacts
from src.quantities import avg_dollar_volume, dollar_volumes
from src.risk.metrics import drift_flag as _drift_flag_check
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import peak_to_trough_pct, position_weight_pct
from src.trading_calendar import et_today, session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")

#: Ceiling on how many symbols get a company-profile lookup for PM's facts
#: block. Profiles are 30-day-cached, so this only bites on a cold cache —
#: but on a cold cache it is one network round trip per symbol, and a
#: pathological candidate list must not be able to turn a nice-to-have
#: identity block into the longest step of the morning session.
_PM_PROFILE_SYMBOL_CAP = 40

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


class PromptFactsMixin:
    """Read-only prompt-context builders mixed into `TradingPipeline`."""

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

    def _build_weekly_narrative(self) -> str:
        """L3a memory: last 7 evenings' daily_summary + daily_pnl, compact."""
        try:
            insights = self.db.get_recent_insights(limit=7)
        except Exception as e:
            logger.warning("weekly_narrative: insights fetch failed: %s", e)
            insights = []
        if not insights:
            return ""
        try:
            pnl_rows = self.db.get_daily_pnl(limit=14)
        except Exception:
            pnl_rows = []
        pnl_by_date = {r["date"]: r for r in pnl_rows}
        lines = []
        # insights come newest-first; display oldest→newest so the "arc" reads naturally
        for row in reversed(insights):
            d = row.get("date", "?")
            summary = (row.get("tomorrow_outlook") or row.get("lessons") or "").strip()
            if len(summary) > 220:
                summary = summary[:217] + "..."
            pnl = pnl_by_date.get(d) or {}
            ret = pnl.get("daily_return_pct")
            ret_str = f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "n/a"
            risk = row.get("risk_rating", "?")
            lines.append(f"- {d}: {ret_str} ({risk}) — {summary}")
        return "\n".join(lines)

    def _build_macro_trajectory(self) -> str:
        """L3b memory: last 7 days of macro regime / confidence / equity outlook.

        No invested target: macro stopped setting one with the owner's
        fully-invested mandate (2026-09-17). Older snapshots still carry
        `position_guidance.target_invested_pct`; it is deliberately not read.
        """
        try:
            history = self.macro_store.load_history(days=7)
        except Exception as e:
            logger.warning("macro_trajectory: load_history failed: %s", e)
            history = []
        if not history:
            return ""
        lines = []
        for snap in history:
            d = snap.get("date", "?")
            regime = snap.get("regime", "?")
            conf = snap.get("confidence", "?")
            outlook = snap.get("equity_outlook") or "?"
            lines.append(f"- {d}: {regime} ({conf}) → outlook {outlook}")
        return "\n".join(lines)

    def _build_active_state_changes(self) -> str:
        """L3c memory: HIGH-conviction state_changes from the last 14 days, deduped."""
        try:
            changes = self.news_store.recent_state_changes(lookback_days=14, limit=8)
        except Exception as e:
            logger.warning("active_state_changes: news_store failed: %s", e)
            changes = []
        if not changes:
            return ""
        lines = []
        for ch in changes:
            d = ch.get("first_seen_date", "?")
            event = (ch.get("event") or "")[:160]
            symbols = ch.get("affected_symbols") or []
            # Phase 13 catalyst-gate fix: render each symbol's direction
            # inline as `SYMBOL(direction)` so `PortfolioManagerAgent.
            # _state_change_symbols_by_date` can parse it back out — this
            # is a round-trip over a format this repo owns end to end
            # (same discipline as the rest of this block). A symbol with
            # no recorded `symbol_direction` (older persisted reports
            # predating this field, or the news analyst genuinely
            # omitting one) renders as `(unknown)`, which the PM-side
            # parser treats as not qualifying — fail closed, never an
            # upgrade to "assume it's good news."
            directions = ch.get("symbol_direction") or {}
            if symbols:
                syms = ", ".join(
                    f"{s.strip().upper()}({directions.get(s.strip().upper(), 'unknown')})"
                    for s in symbols[:6]
                )
            else:
                syms = "—"
            lines.append(f"- [{d}] {event} → {syms}")
        return "\n".join(lines)

    def _build_rm_recent_verdicts(self, limit: int = 5) -> str:
        """How RM has been judging PM's output over the last N sessions.

        PM reading this lets it self-calibrate: if RM has been scaling BUYs
        down for several runs in a row, PM has been oversizing — pull base
        allocations down before RM has to do it again.
        """
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("rm_recent_verdicts: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L5 layer reads RM history to self-calibrate.
                # Silently dropping a corrupt full_response row makes PM
                # see fewer verdicts than the DB actually contains and
                # the operator never knows. Surface it so a recurring
                # corruption pattern shows up in logs.
                logger.warning(
                    "rm_recent_verdicts: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            approved = data.get("approved")
            mods = data.get("modifications") or []
            scale = data.get("scale_all_buys", 1.0)
            try:
                scale = float(scale) if scale is not None else 1.0
            except (TypeError, ValueError):
                scale = 1.0
            verdict = "APPROVED" if approved else "REJECTED"
            category = (data.get("reason_category") or "clean").strip()
            extras: list[str] = [f"cat={category}"]
            if scale < 1.0:
                extras.append(f"scale_all_buys={scale:.2f}")
            if mods:
                mod_syms = sorted({m.get("symbol", "?") for m in mods if isinstance(m, dict)})
                if mod_syms:
                    extras.append(f"mods on {', '.join(mod_syms)}")
            # Phase 10.1 — a per-symbol refusal is the sharpest feedback this
            # loop can carry: `reason_category` alone tells PM the plan had an
            # R/R problem, this tells it which NAME died for it. Rendered as
            # plain text from the stored verdict, tolerant of any shape,
            # because a display line must never raise on a historical row.
            rejected = data.get("rejected_symbols") or []
            if isinstance(rejected, list):
                rej_syms = sorted({
                    (r.get("symbol") if isinstance(r, dict) else r)
                    for r in rejected
                    if isinstance(r, (dict, str))
                } - {None, ""})
                if rej_syms:
                    extras.append(f"refused {', '.join(str(s) for s in rej_syms)}")
            tag = f" [{'; '.join(extras)}]"
            reason = (data.get("reasoning") or "")[:140].strip().replace("\n", " ")
            lines.append(f"- {ts}: {verdict}{tag} — {reason}")
        return "\n".join(lines)

    def _build_pm_recent_decisions(self, limit: int = 3) -> str:
        """PM's own last N decision sets — used to spot flip-flopping against itself."""
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="portfolio_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L6 layer reads its own recent decision history to
                # spot flip-flops. A silent skip on JSON corruption hides
                # the gap; same fix as L5 / L3d / L3f builders.
                logger.warning(
                    "pm_recent_decisions: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            # Phase 2: new schema emits `targets` (target weights + thesis);
            # older logs in the DB carry `decisions` (legacy TradeDecision).
            # Parse whichever is present so PM reads a unified history.
            targets = data.get("targets") or []
            decisions = data.get("decisions") or []
            summary_parts: list[str] = []
            if targets:
                for t in targets[:8]:
                    if not isinstance(t, dict):
                        continue
                    sym = t.get("symbol", "?")
                    # The live schema sizes a target by `risk_allocation_pct`;
                    # `target_weight_pct` is the legacy notional field older
                    # logs carry. Reading only the legacy one rendered every
                    # recent size as "?", and a flip-flop check that cannot
                    # see the size is not a check. Tag the unit — 1% of risk
                    # and 1% of notional are not the same number.
                    risk = t.get("risk_allocation_pct")
                    weight = t.get("target_weight_pct")
                    if risk is not None:
                        w = f"{risk}%r"
                    elif weight is not None:
                        w = f"{weight}%w"
                    else:
                        w = "?"
                    conv = (t.get("conviction") or "?")[0]
                    summary_parts.append(f"{sym}→{w}({conv})")
            elif decisions:
                for d in decisions[:8]:
                    if not isinstance(d, dict):
                        continue
                    act = d.get("action", "?")
                    sym = d.get("symbol", "?")
                    alloc = d.get("allocation_pct", "?")
                    summary_parts.append(f"{act} {sym} {alloc}%")
            if not summary_parts:
                lines.append(f"- {ts}: (no trades that day)")
                continue
            rc = data.get("reasoning_chain") or {}
            sizing = (rc.get("sizing_logic") or "")[:160].strip().replace("\n", " ")
            continuity = (rc.get("continuity_check") or "")[:160].strip().replace("\n", " ")
            line = f"- {ts}: {'; '.join(summary_parts)}"
            if sizing:
                line += f"\n    sizing: {sizing}"
            if continuity:
                line += f"\n    continuity: {continuity}"
            lines.append(line)
        return "\n".join(lines)

    def _build_projected_portfolio(
        self,
        positions,
        analyses: list[TechAnalysisResult],
        total_value: float,
    ) -> str:
        """Preview of the book if PM rubber-stamped every BUY-rated TA candidate.

        Surfaces sector concentration BEFORE PM writes decisions, so it can
        self-correct instead of waiting for RM or the hard sector cap to flag
        it. Kept simple on purpose: no correlation math here (that's RM's
        correlation_cluster advisory). Just current vs projected sector mix.

        BOARD ITEM 221 — every candidate used to be previewed at a FLAT
        `default_buy_pct=5.0`, a per-candidate constant nothing else in the
        desk used, and the projected sector mix was built by summing it.
        The constructor sizes each name from its OWN stop distance, so the
        mix shown here was a book no candidate would ever be given.

        THIS PREVIEW IS STRUCTURALLY INCAPABLE OF PROJECTING REALISED
        SECTOR WEIGHTS, AND NO LONGER CLAIMS ONE. The constructor ships
        the SMALLEST of several limits: the PM's target delta, the name's
        stop-implied ceiling, and the further limits on how much of one
        name and one sector the desk may hold, whose interaction is itself
        unsettled (board item 222). The PM
        target delta is the PM's own decision, and this preview is an INPUT
        to that decision — it is built BEFORE the PM writes a target. So no
        candidate's eventual weight is knowable here, and every projected
        mix this function could print would be a guess. Sizing each
        candidate at the largest size its stop permits is NOT the fix: with
        the desk's ordinary stop widths that reaches the single-name
        ceiling and projects sector weights in the hundreds of percent — a
        more confident fiction than the flat slice, correcting the PM for a
        concentration that cannot occur.

        What it reports instead is parameter-free and all of it is known at
        this moment: the held book's measured sector weights, the SECTOR
        COMPOSITION of the candidate set (which names fall in which sector,
        and how many), and per candidate its own stop distance and the
        ceiling that stop implies through `risk_budget_allocation_pct` —
        the SAME single definition `PortfolioConstructor._build_buy` caps
        with — CLAMPED to the single-name notional ceiling so the figure on
        the page is one the desk could actually reach. A ceiling on one
        name is a property of that trade; it is not a weight and is never
        summed into one. Each sector's share OF THE CANDIDATE SET is stated
        as the forward fact, measured rather than projected, with no
        threshold and no warning level attached to it.

        If you are here to "fix" the preview so it projects a mix again:
        read the paragraph above first. The number you would need is a
        decision nobody has made yet.
        """
        from src.execution.broker import _get_sector
        from src.portfolio_constructor import ConstructorConfig
        from src.risk.constants import risk_budget_allocation_pct
        from src.risk.rules import book_exposure, sector_side_gross
        if total_value <= 0:
            return ""
        buy_candidates = [
            a for a in analyses
            if a.rating in ("buy", "strong_buy") and a.entry_price
        ]
        if not positions and not buy_candidates:
            return ""

        cached_sectors = dict(getattr(self, "_last_symbol_sectors", {}))

        def _resolve_sector(symbol: str, fallback: str | None = None) -> str:
            sector = (fallback or "").strip() if fallback else ""
            if sector and sector != "Unknown":
                cached_sectors[symbol] = sector
                return sector

            sector = cached_sectors.get(symbol, "")
            if sector and sector != "Unknown":
                return sector

            sector = _get_sector(symbol) or "Unknown"
            if sector != "Unknown":
                cached_sectors[symbol] = sector
            return sector

        # Same `book_exposure` the PM's Account Status, the PMFacts Book
        # State block and the pre-trade advisory read. This preview used to
        # carry its own `abs(sum(mv * signed_mult))` — a fourth number for
        # the one quantity, in the same prompt as the other three, and the
        # `abs()` made a net-SHORT book render as positively invested.
        current_book = book_exposure(positions, total_value)
        current_invested_pct = current_book.deployed_pct
        current_net = current_book.net_usd
        # Spec §12.2 — GROSS (unsigned) and split by side, keyed
        # `(sector, side)`. Before §12.2 this summed SIGNED `market_value`
        # exactly as the gate did, so a held short shrank its sector in the
        # very preview whose job is to surface concentration.
        sector_gross: dict[tuple[str, str], float] = sector_side_gross(
            positions,
            resolve_sector=lambda p: _resolve_sector(p.symbol, p.sector),
            include_unknown=True,
        )

        unresolved_symbols: list[str] = []
        unsized_symbols: list[str] = []
        # The constructor's own sizing dials, read off the constructor the
        # pipeline actually builds orders with, so the preview cannot drift
        # from it. The fallbacks are `ConstructorConfig`'s own ratified
        # defaults, not numbers chosen here, and they are reached only when
        # no constructor is attached (a bare pipeline in a test) or when a
        # MagicMock config auto-creates a non-numeric attribute.
        cstr_cfg = getattr(
            getattr(self, "portfolio_constructor", None), "cfg", None,
        )

        def _dial(name: str) -> float:
            """One of the constructor's own sizing dials, read off the
            constructor the pipeline really builds orders with.

            The fallback is `ConstructorConfig`'s OWN declared default for
            that same field — never a literal written here. A literal at
            this call site is a flat number the number-source scanner
            cannot see (it reads definition sites, not positional call
            arguments), so it would silently desync the text the PM reads
            from what the constructor does the next time the default moves.
            Reached only when no constructor is attached (a bare pipeline
            in a test) or when a MagicMock config auto-creates a
            non-numeric attribute.
            """
            default = getattr(ConstructorConfig(), name)
            raw = getattr(cstr_cfg, name, None)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return float(default)
            return float(raw) if raw > 0 else float(default)

        risk_budget_pct = _dial("risk_budget_pct")
        max_position_pct = _dial("max_position_pct")
        by_sector: dict[str, list[str]] = {}
        per_candidate: list[str] = []
        for a in buy_candidates:
            entry = float(a.entry_price)
            # The candidate's OWN stop-implied ceiling, through the one
            # shared definition the constructor caps a long with. It is a
            # CEILING on this one name, not a weight: nothing here knows
            # what the PM will ask for, so nothing here may claim a size.
            ceiling_pct = risk_budget_allocation_pct(
                entry_price=entry,
                stop_price=a.stop_loss if a.stop_loss is not None else 0.0,
                total_value=total_value,
                risk_budget_pct=risk_budget_pct,
            )
            sec = _resolve_sector(a.symbol)
            if sec == "Unknown":
                unresolved_symbols.append(a.symbol)
            by_sector.setdefault(sec, []).append(a.symbol)
            if ceiling_pct is None or ceiling_pct <= 0 or entry <= 0:
                # No usable stop geometry: NAMED, never back-filled with an
                # assumed size. Inventing one is the defect this preview had.
                unsized_symbols.append(a.symbol)
                continue
            stop_distance_pct = abs(entry - float(a.stop_loss)) / entry * 100
            # CLAMPED to the single-name notional ceiling. An UNREACHABLE
            # number printed beside a sector label is an invitation to add
            # up, whatever the sentence beside it says — unclamped, the
            # desk's ordinary stop widths print things like "≤247%", which
            # is the rejected 225% arithmetic in another costume.
            reachable_pct = min(ceiling_pct, max_position_pct)
            per_candidate.append(
                f"{a.symbol} stop -{stop_distance_pct:.1f}% "
                f"→ ≤{reachable_pct:.0f}%"
            )
        self._last_symbol_sectors = cached_sectors

        def _sector_line(sector_dict: dict[tuple[str, str], float]) -> str:
            if not sector_dict:
                return "(empty)"
            sorted_secs = sorted(sector_dict.items(), key=lambda kv: -kv[1])[:5]
            return ", ".join(
                f"{sec} {side} {v / total_value * 100:.0f}%"
                for (sec, side), v in sorted_secs
            )

        lines = [
            f"- Current: {current_invested_pct:.0f}% invested (capital at work) · "
            f"net direction {current_book.net_pct:+.0f}% · sectors: {_sector_line(sector_gross)}",
        ]
        # Spec §12.2/§12.3 — the concentration target comes from the SAME
        # `max_sector_pct` the constructor sizes against and the gate
        # measures against, so the preview cannot warn about a line the rest
        # of the system does not draw. It is applied to the HELD book, which
        # is measured; it is no longer applied to a projected book, which
        # cannot be computed here (see below).
        target_pct = getattr(
            getattr(self, "risk_engine", None), "config", None,
        )
        target_pct = getattr(target_pct, "max_sector_pct", None) or 75.0
        overweight = [
            f"{sec} ({side})" for (sec, side), v in sector_gross.items()
            if v / total_value * 100 > target_pct and sec != "Unknown"
        ]
        if overweight:
            lines.append(
                f"    ⚠ Held sector sides already over the {target_pct:.0f}% "
                f"concentration target (each further trade there is scaled "
                f"down, not refused): {', '.join(sorted(overweight))}"
            )
        if buy_candidates:
            # Each sector's share OF THE CANDIDATE SET. This is a MEASURED
            # forward fact — "6 of 9 candidates are Technology" is true of
            # what is on offer right now and needs no projection, no
            # assumed size and no invented threshold. It is deliberately
            # stated WITHOUT a warning level: the seat judges it. Without
            # it, six candidates in one sector against nothing held read as
            # a bare count and the PM's own ordering and dropping decision
            # — the thing this preview exists to inform — is unguided.
            total_candidates = len(buy_candidates)
            composition = ", ".join(
                f"{sec} {len(syms)} of {total_candidates} "
                f"({len(syms) / total_candidates * 100:.0f}% of the candidate "
                f"set: {', '.join(syms[:6])}"
                + (f" +{len(syms) - 6} more" if len(syms) > 6 else "")
                + ")"
                for sec, syms in sorted(
                    by_sector.items(), key=lambda kv: (-len(kv[1]), kv[0]),
                )
            )
            lines.append(
                f"- {total_candidates} BUY-rated candidate(s) on offer, "
                f"by sector: {composition}"
            )
            if per_candidate:
                n = len(per_candidate)
                shown = per_candidate[:8]
                tail = f" +{n - 8} more" if n > 8 else ""
                lines.append(
                    "- Per candidate, its OWN stop distance and the ceiling "
                    f"that stop implies at the {risk_budget_pct:.1f}% risk "
                    f"budget — a cap on ONE name, NOT a weight: "
                    f"{'; '.join(shown)}{tail}"
                )
                lines.append(
                    f"    (already clamped to the {max_position_pct:.0f}% "
                    "single-name notional ceiling, which is ONE of several "
                    "limits on how much of one name the desk may hold; "
                    "which limit actually binds is unsettled — board item "
                    "222 — so treat each figure as an upper bound that may "
                    "be cut further, never as an entitlement)"
                )
            if unsized_symbols:
                lines.append(
                    "    ⚠ No usable stop geometry, so no ceiling can be "
                    "stated and none is assumed: "
                    f"{', '.join(dict.fromkeys(unsized_symbols))}"
                )
            if unresolved_symbols:
                unique = list(dict.fromkeys(unresolved_symbols))
                lines.append(
                    "    ⚠ Sector unresolved for: "
                    f"{', '.join(unique)} — the composition above may "
                    "understate how crowded one sector is."
                )
            lines.append(
                "- This preview CANNOT tell you what these candidates would "
                "weigh as a share of the book, and does not try. The "
                "constructor sizes each name at the SMALLEST of several "
                "limits — the target weight YOU write, that name's "
                "stop-implied ceiling, and further limits on how much of "
                "one name and one sector the desk may hold (board item 222: "
                "which of them binds is unsettled) — and your targets do "
                "not exist yet, because this preview is an INPUT to the "
                "decision you are about to make. Judge crowding from the "
                "held weights and the candidate composition above."
            )
        return "\n".join(lines)

    def _build_recent_sells_for_grading(
        self, lookback_days: int = 2,
        symbols_bars: dict | None = None,
    ) -> list[dict]:
        """Return recent SELL-family trades joined with current quote for grading.

        Used by evening to produce `sell_decisions_assessment`. For each SELL
        in the window, we fetch the current price and compute pct move since
        the sell — positive means we left money on the table, negative means
        the exit saved capital. Broker lookup errors fall back to 0% (log).
        """
        try:
            all_rows = self.db.get_trades(limit=200, executed_only=True)
        except Exception as e:
            logger.warning("recent_sells: db fetch failed: %s", e)
            return []
        if not all_rows:
            return []
        from datetime import date as _date, timedelta as _td
        cutoff = et_today() - _td(days=lookback_days)
        # REDUCE = midday reviewer trim (discretionary partial exit — a SELL
        # decision the reviewer owns and should be graded on). TAKE_PROFIT
        # stays out: it was the rule-based auto trim (deleted 2026-09-12),
        # never a reviewer decision — historical rows still carry the label.
        # Belt (audit round 2): the vehicle also exits under EMERGENCY_SELL
        # when the breaker liquidates everything — filter by SYMBOL here,
        # mirroring _build_post_exit_reality, so parking churn never reaches
        # the grading loop under any action name.
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        sell_actions = ("SELL", "EMERGENCY_SELL", "FORCE_DELEVER", "REDUCE")
        out: list[dict] = []
        for row in all_rows:
            action = row.get("action") or ""
            if not (action in sell_actions or action.startswith("PARTIAL_SELL")):
                continue
            ts = row.get("timestamp") or ""
            try:
                sell_date = _date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if sell_date < cutoff:
                continue
            sym = row.get("symbol")
            if sweep_symbol is not None and sym == sweep_symbol:
                continue   # parking churn is not a graded decision
            sell_price = float(row.get("fill_price") or row.get("price") or 0) or 0.0
            if not sym or sell_price <= 0:
                continue
            # Current price: prefer live broker quote; degrade to position map;
            # degrade to last known OHLCV close.
            curr = 0.0
            try:
                curr = float(self.broker.get_latest_price(sym) or 0) or 0.0
            except Exception as e:
                logger.warning("recent_sells: latest price failed for %s: %s", sym, e)
            if curr <= 0:
                bars = (symbols_bars or {}).get(sym) or []
                if bars:
                    curr = float(bars[-1].close or 0)
            pct = ((curr / sell_price - 1) * 100) if (curr > 0 and sell_price > 0) else 0.0
            out.append({
                "symbol": sym,
                "sell_date": str(sell_date),
                "sell_price": sell_price,
                "current_price": round(curr, 2) if curr else 0.0,
                "pct_move_since_sell": round(pct, 2),
                "reasoning": row.get("reasoning") or "",
            })
        # Newest first, cap to avoid bloating the evening prompt
        out.sort(key=lambda r: r["sell_date"], reverse=True)
        return out[:10]

    def _build_recent_buys_for_grading(
        self, lookback_days: int = 5,
        symbols_bars: dict | None = None,
    ) -> list[dict]:
        """Mirror of `_build_recent_sells_for_grading` for entry quality.

        For each executed BUY in the window, compute the pct move since
        entry vs current price. Positive = entry still in the money (so
        far); negative = entry is underwater. Lookback is wider than
        SELLs (5d vs 2d) because BUY outcomes take longer to reveal.

        Also injects `market_relative_move_pct` per BUY = (our move) −
        (SPY move over same dates). The evening analyst reads this to
        decide whether a losing BUY was alpha-destruction (we
        under-performed the tape, positive number) vs systemic drawdown
        (market also fell, ~0 or negative number). Fetched once upfront
        so we don't round-trip SPY bars per BUY.
        """
        try:
            all_rows = self.db.get_trades(limit=200, executed_only=True)
        except Exception as e:
            logger.warning("recent_buys: db fetch failed: %s", e)
            return []
        if not all_rows:
            return []
        from datetime import date as _date, timedelta as _td
        cutoff = et_today() - _td(days=lookback_days)
        # SPY bars once — used to compute market_relative_move_pct per BUY.
        # Pad the lookback to cover the oldest BUY date + weekends.
        spy_close_by_date: dict[str, float] = {}
        spy_latest_close: float = 0.0
        try:
            spy_bars = self.market.get_ohlcv(
                "SPY", lookback_days=max(lookback_days + 5, 12)
            )
            for b in spy_bars or []:
                try:
                    spy_close_by_date[str(b.date)] = float(b.close)
                except (AttributeError, TypeError, ValueError):
                    continue
            if spy_bars:
                try:
                    spy_latest_close = float(spy_bars[-1].close)
                except (AttributeError, TypeError, ValueError):
                    spy_latest_close = 0.0
        except Exception as e:
            logger.warning("recent_buys: SPY bars fetch failed (relative-move disabled): %s", e)
        # audit round 2: get_ohlcv returns COMPLETED bars only, so during
        # market hours they stop at the previous close — while the stock leg
        # uses a LIVE quote. For a
        # same-day BUY that mismatch made spy_pct read 0.0 and every
        # market_relative grade compare a live price against a stale
        # benchmark. Same-instant legs: prefer the live SPY quote.
        try:
            spy_live = float(self.broker.get_latest_price("SPY") or 0) or 0.0
            if spy_live > 0:
                spy_latest_close = spy_live
        except Exception as e:  # noqa: BLE001
            logger.warning("recent_buys: live SPY quote failed (using last close): %s", e)
        out: list[dict] = []
        seen_symbols: set[str] = set()  # dedupe multiple buys on same symbol — use latest
        for row in all_rows:
            action = (row.get("action") or "").upper()
            if action != "BUY":
                continue
            ts = row.get("timestamp") or ""
            try:
                buy_date = _date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if buy_date < cutoff:
                continue
            sym = row.get("symbol")
            buy_price = float(row.get("fill_price") or row.get("price") or 0) or 0.0
            if not sym or buy_price <= 0:
                continue
            if sym in seen_symbols:
                continue  # only surface latest BUY per symbol
            seen_symbols.add(sym)
            curr = 0.0
            try:
                curr = float(self.broker.get_latest_price(sym) or 0) or 0.0
            except Exception as e:
                logger.warning("recent_buys: latest price failed for %s: %s", sym, e)
            if curr <= 0:
                bars = (symbols_bars or {}).get(sym) or []
                if bars:
                    curr = float(bars[-1].close or 0)
            pct = ((curr / buy_price - 1) * 100) if (curr > 0 and buy_price > 0) else 0.0
            # SPY return over the same window → alpha-destruction vs systemic
            # drawdown disambiguation. Match buy_date to the nearest SPY close
            # (buy_date might not be a trading day if fill timestamp rolled
            # over into an ET weekend), walking backward up to 5 days.
            spy_entry_close = 0.0
            if spy_close_by_date and spy_latest_close > 0:
                probe = buy_date
                for _ in range(6):
                    got = spy_close_by_date.get(str(probe))
                    if got:
                        spy_entry_close = got
                        break
                    probe = probe - _td(days=1)
            if spy_entry_close > 0 and spy_latest_close > 0:
                spy_pct = (spy_latest_close / spy_entry_close - 1) * 100
                market_relative = round(pct - spy_pct, 2)
            else:
                market_relative = None
            out.append({
                "symbol": sym,
                "buy_date": str(buy_date),
                "buy_price": buy_price,
                "current_price": round(curr, 2) if curr else 0.0,
                "pct_move_since_buy": round(pct, 2),
                "market_relative_move_pct": market_relative,
                "reasoning": row.get("reasoning") or "",
            })
        out.sort(key=lambda r: r["buy_date"], reverse=True)
        return out[:10]

    def _build_recent_outlook_calibration(self, lookback: int = 10) -> dict:
        """Evening's self-calibration — pairs its own past `tomorrow_bias`
        predictions with the actual next-day return from daily_pnl.

        Returns a dict:
        {
          "samples": [{date, predicted_bias, predicted_conviction,
                       actual_return_pct, matched: bool}, ...],
          "bullish_hit_rate": float | None,
          "bearish_hit_rate": float | None,
          "high_conviction_hit_rate": float | None,
          "n": int,
        }
        Empty / None when there aren't enough pairs (first N days of run).

        "Matched" for bullish = actual > 0, bearish = actual < 0, neutral =
        within ±0.3%. This gives evening a deterministic mirror of its own
        accuracy — it can't bullshit itself into pretending it's been right
        when the numbers say otherwise.
        """
        try:
            insights = self.db.get_recent_insights(limit=lookback + 5)
        except Exception as e:
            logger.warning("outlook_calibration: insights fetch failed: %s", e)
            return {"samples": [], "n": 0}
        if not insights:
            return {"samples": [], "n": 0}
        try:
            pnl_rows = self.db.get_daily_pnl(limit=lookback + 10)
        except Exception as e:
            logger.warning("outlook_calibration: daily_pnl fetch failed: %s", e)
            return {"samples": [], "n": 0}
        pnl_by_date = {r["date"]: r.get("daily_return_pct") for r in (pnl_rows or [])}

        # Ordered trading-day series for multi-day (trend) forward returns. The
        # next-day return is NOISE in a trending tape (flat up-days score a
        # bullish call as a "miss"); a 5-session forward cumulative return is
        # the directional scorecard evening should actually weigh, so it stops
        # mis-learning a low next-day hit rate into "default neutral".
        import bisect
        _ordered = sorted(
            ((d, r) for d, r in pnl_by_date.items() if r is not None),
            key=lambda x: x[0],
        )
        _ordered_dates = [d for d, _ in _ordered]

        def _fwd_cumulative(pred_date_str: str, n: int = 5):
            """Sum daily_return_pct over the first n trading days STRICTLY
            after pred_date_str. Returns None unless the FULL n-session window
            has resolved — a partial window (e.g. 1 of 5 days for a very recent
            prediction) is just a relabeled next-day return, not a trend, so we
            withhold it rather than feed a misleading number."""
            i = bisect.bisect_right(_ordered_dates, pred_date_str)
            window = _ordered[i:i + n]
            if len(window) < n:
                return None
            return sum(r for _, r in window)

        from datetime import date as _date, timedelta as _td
        samples: list[dict] = []
        for ins in insights:
            pred_date_str = ins.get("date")
            if not pred_date_str:
                continue
            try:
                pred_date = _date.fromisoformat(pred_date_str)
            except ValueError:
                continue
            # tomorrow_bias written on day D predicts day D+1's direction.
            # But "D+1" has to be a trading day — so we find the NEXT daily_pnl
            # row after pred_date. Simplest: try +1, +2, +3 days until hit.
            actual = None
            for delta in (1, 2, 3, 4):
                cand = str(pred_date + _td(days=delta))
                if cand in pnl_by_date:
                    actual = pnl_by_date[cand]
                    break
            if actual is None:
                continue

            bias = (ins.get("tomorrow_bias") or "neutral").lower()
            conv = (ins.get("tomorrow_conviction") or "medium").lower()
            # Match rule:
            NEUTRAL_BAND = 0.3
            if bias == "bullish":
                matched = actual > NEUTRAL_BAND
            elif bias == "bearish":
                matched = actual < -NEUTRAL_BAND
            else:  # neutral
                matched = -NEUTRAL_BAND <= actual <= NEUTRAL_BAND
            # 5-session forward cumulative return — the trend/direction metric.
            fwd5 = _fwd_cumulative(pred_date_str, 5)
            TREND_BAND = 0.75  # wider neutral band over 5 sessions than the 1d 0.3
            if fwd5 is None:
                trend_matched = None
            elif bias == "bullish":
                trend_matched = fwd5 > TREND_BAND
            elif bias == "bearish":
                trend_matched = fwd5 < -TREND_BAND
            else:  # neutral
                trend_matched = -TREND_BAND <= fwd5 <= TREND_BAND
            samples.append({
                "date": pred_date_str,
                "predicted_bias": bias,
                "predicted_conviction": conv,
                "actual_return_pct": round(actual, 2),
                "matched": bool(matched),
                "fwd5_return_pct": round(fwd5, 2) if fwd5 is not None else None,
                "trend_matched": (None if trend_matched is None else bool(trend_matched)),
            })
            if len(samples) >= lookback:
                break

        n = len(samples)
        def _rate(filter_fn):
            eligible = [s for s in samples if filter_fn(s)]
            if not eligible:
                return None
            return round(100 * sum(1 for s in eligible if s["matched"]) / len(eligible), 1)

        def _trend_rate(filter_fn):
            # Only over samples with a resolved 5-session forward window.
            eligible = [s for s in samples if filter_fn(s) and s.get("trend_matched") is not None]
            if not eligible:
                return None
            return round(100 * sum(1 for s in eligible if s["trend_matched"]) / len(eligible), 1)

        return {
            "samples": samples,
            "n": n,
            # Next-day hit rates — NOISE filter; do not read as a directional verdict.
            "overall_hit_rate_pct": _rate(lambda s: True),
            "bullish_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "bullish"),
            "bearish_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "bearish"),
            "neutral_hit_rate_pct": _rate(lambda s: s["predicted_bias"] == "neutral"),
            "high_conviction_hit_rate_pct": _rate(lambda s: s["predicted_conviction"] == "high"),
            "low_conviction_hit_rate_pct": _rate(lambda s: s["predicted_conviction"] == "low"),
            # 5-session forward (trend) hit rates — the real directional scorecard.
            "overall_trend_hit_rate_pct": _trend_rate(lambda s: True),
            "bullish_trend_hit_rate_pct": _trend_rate(lambda s: s["predicted_bias"] == "bullish"),
            "bearish_trend_hit_rate_pct": _trend_rate(lambda s: s["predicted_bias"] == "bearish"),
        }

    def _build_trade_grade_summary(self, lookback_days: int = 14) -> dict:
        """Aggregate evening's structured sell_grades + buy_grades over N days.

        Feeds position_reviewer so it can see patterns like "you marked 5 of
        7 recent SELLs as premature" and lean patient today. Reads the new
        JSON columns on insights (introduced 2026-04-19); pre-v2 rows return
        NULL → treated as empty, summary gracefully degrades.

        Returns {
            "n_sells": int, "n_buys": int,
            "sell_counts": {"correct": int, "premature": int, "wrong": int},
            "buy_counts":  {"correct": int, "premature": int, "wrong": int},
            "repeat_premature_symbols": [str, ...],   # symbol premature >= 2×
            "repeat_wrong_symbols":     [str, ...],
        }
        """
        import json as _json
        empty = {
            "n_sells": 0, "n_buys": 0,
            "sell_counts": {"correct": 0, "premature": 0, "wrong": 0},
            "buy_counts":  {"correct": 0, "premature": 0, "wrong": 0},
            "repeat_premature_symbols": [],
            "repeat_wrong_symbols": [],
        }
        def _with_reality(base: dict) -> dict:
            # The deterministic post-exit block must ride along even when
            # nightly grades are absent/corrupt — it's tape-derived, not
            # grade-derived, and it's the part the grader can't sugar-coat.
            try:
                base["post_exit_reality"] = self._build_post_exit_reality(
                    lookback_days=max(lookback_days, 14),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("post_exit_reality failed (summary degrades): %s", e)
                base["post_exit_reality"] = None
            return base

        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("trade_grade_summary: insights fetch failed: %s", e)
            return _with_reality(empty)
        if not rows:
            return _with_reality(empty)

        sell_counts = {"correct": 0, "premature": 0, "wrong": 0}
        buy_counts = {"correct": 0, "premature": 0, "wrong": 0}
        sell_premature_by_symbol: dict[str, int] = {}
        sell_wrong_by_symbol: dict[str, int] = {}

        def _load(col: str, row: dict) -> list[dict]:
            raw = row.get(col)
            if not raw:
                return []
            try:
                v = _json.loads(raw)
            except (TypeError, ValueError) as exc:
                # Silent degradation here previously hid real data loss — if
                # evening wrote grades but they can't be parsed back, the
                # position_reviewer was reading n_sells=0 and silently losing
                # the SELL-discipline feedback loop. Warn loudly so the next
                # evening run can regenerate and we can see the symptom.
                preview = (raw if isinstance(raw, str) else str(raw))[:120]
                logger.warning(
                    "_build_trade_grade_summary: failed to parse insights[%s] "
                    "(row date=%s): %s — preview=%r",
                    col, row.get("date", "?"), exc, preview,
                )
                return []
            if not isinstance(v, list):
                logger.warning(
                    "_build_trade_grade_summary: insights[%s] (row date=%s) "
                    "expected list, got %s — ignoring",
                    col, row.get("date", "?"), type(v).__name__,
                )
                return []
            return v

        rows_in_window = rows[:lookback_days]  # newest first from get_recent_insights
        # One SELL, one vote. `_build_recent_sells_for_grading` uses a 2-day
        # window with no already-graded filter, so evening re-grades the same
        # trade on 2-3 consecutive nights and each re-grade used to count as an
        # independent sell — inflating the premature/wrong counts that drive
        # the reviewer's patience tilt (2026-07-16 audit; the production
        # insights rows show the duplicates). Rows arrive newest-first, so the
        # FIRST grade seen for a (symbol, sell_date) is the freshest — and the
        # one with the most post-exit price history behind it.
        seen_sells: set[tuple] = set()
        for row in rows_in_window:
            for g in _load("sell_grades_json", row):
                if not isinstance(g, dict):
                    continue
                sym = g.get("symbol")
                sell_date = g.get("sell_date")
                # Dedup only with a real (symbol, sell_date) key — SellGrade
                # requires sell_date, so this is the normal path. A malformed
                # row without one is counted rather than collapsed: keying on
                # (symbol, None) would fold every distinct sell of that symbol
                # into a single vote, which is a worse error than the
                # double-count this dedup removes.
                if sym and sell_date:
                    key = (sym, sell_date)
                    if key in seen_sells:
                        continue
                    seen_sells.add(key)
                grade = g.get("grade")
                if grade in sell_counts:
                    sell_counts[grade] += 1
                if sym and grade == "premature":
                    sell_premature_by_symbol[sym] = sell_premature_by_symbol.get(sym, 0) + 1
                if sym and grade == "wrong":
                    sell_wrong_by_symbol[sym] = sell_wrong_by_symbol.get(sym, 0) + 1
            for g in _load("buy_grades_json", row):
                if not isinstance(g, dict):
                    continue
                grade = g.get("grade")
                if grade in buy_counts:
                    buy_counts[grade] += 1

        summary = {
            "n_sells": sum(sell_counts.values()),
            "n_buys": sum(buy_counts.values()),
            "sell_counts": sell_counts,
            "buy_counts": buy_counts,
            "repeat_premature_symbols": sorted(
                s for s, c in sell_premature_by_symbol.items() if c >= 2
            ),
            "repeat_wrong_symbols": sorted(
                s for s, c in sell_wrong_by_symbol.items() if c >= 2
            ),
        }
        # RC4 (2026-07-16): deterministic post-exit reality. The LLM grader
        # scored 32/33 recent sells "correct" at t+1..t+3 while the tape
        # showed 28/53 exits ≥5% higher within 20 days — self-assessment
        # cannot be the only input to the patience tilt. These numbers come
        # from trades × live prices, no LLM in the loop.
        return _with_reality(summary)

    def _build_post_exit_reality(
        self, lookback_days: int = 14, min_age_days: int = 2, max_symbols: int = 12,
    ) -> dict | None:
        """What actually happened after our recent exits — from the tape.

        For every realized exit in the window (SELL family + filled
        TRAIL_STOPs) at least `min_age_days` old, compare the exit price to
        the live price. Returns None when there's nothing to audit.

        {"n": int, "n_higher_5pct": int, "avg_move_pct": float,
         "worst": [{"symbol", "date", "move_pct"} × ≤3]}   # worst = ran most
        """
        from datetime import datetime as _dt, timedelta, timezone
        try:
            rows = self.db.get_trades(limit=120)
        except Exception as e:  # noqa: BLE001
            logger.warning("post_exit_reality: trades fetch failed: %s", e)
            return None
        now = _dt.now(timezone.utc)
        window_start = now - timedelta(days=lookback_days)
        age_cutoff = now - timedelta(days=min_age_days)
        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None
        exits: list[dict] = []
        for row in rows:
            action = (row.get("action") or "").upper()
            # Belt on top of the SWEEP_* action exclusion: an emergency
            # liquidation can exit the sweep vehicle under EMERGENCY_SELL —
            # a ~0% T-bill "move" is noise in a decision-quality audit.
            if sweep_symbol is not None and (row.get("symbol") or "") == sweep_symbol:
                continue
            is_exit = (
                action in self._EXIT_AUDIT_ACTIONS
                or action.startswith("PARTIAL_SELL")
                or (action == "TRAIL_STOP"
                    and (row.get("fill_status") or "") == "filled")
            )
            if not is_exit:
                continue
            if action != "TRAIL_STOP" and (row.get("fill_status") or "") not in (
                "filled", "submitted",
            ):
                continue
            ts = row.get("timestamp") or ""
            try:
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts \
                    else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not (window_start <= dt <= age_cutoff):
                continue
            exit_px = row.get("fill_price") or row.get("price")
            if not (isinstance(exit_px, (int, float)) and exit_px > 0):
                continue
            exits.append({
                "symbol": row.get("symbol"), "date": ts[:10],
                "exit_px": float(exit_px),
            })
        if not exits:
            return None
        # Live prices — one broker call per distinct symbol, capped.
        prices: dict[str, float] = {}
        for sym in list(dict.fromkeys(e["symbol"] for e in exits))[:max_symbols]:
            try:
                px = self.broker.get_latest_price(sym)
            except Exception:  # noqa: BLE001
                px = None
            if isinstance(px, (int, float)) and px > 0:
                prices[sym] = float(px)
        moves: list[dict] = []
        for e in exits:
            cur = prices.get(e["symbol"])
            if cur is None:
                continue
            moves.append({
                "symbol": e["symbol"], "date": e["date"],
                "move_pct": round((cur - e["exit_px"]) / e["exit_px"] * 100, 1),
            })
        if not moves:
            return None
        moves.sort(key=lambda m: -m["move_pct"])
        return {
            "n": len(moves),
            "n_higher_5pct": sum(1 for m in moves if m["move_pct"] >= 5.0),
            "avg_move_pct": round(sum(m["move_pct"] for m in moves) / len(moves), 1),
            "worst": moves[:3],
        }

    def _build_recent_missed_lessons(self, lookback_days: int = 14) -> str:
        """PM L3d memory: themes that evening flagged ≥ 2 times as missed.

        Reads `insights.missed_opportunities_json` for the last N days, skips
        the two "not-really-a-miss" categories (noise_rally, risk_disciplined),
        groups by `theme_if_any` (falling back to `symbol` when no theme
        tagged), keeps themes seen on 2+ distinct dates. Output is prose
        PM renders directly — the whole point of this memory layer is PM
        sees "nuclear/power keeps showing up — am I blind to it?" before
        deciding today's positions.

        Empty string when there's nothing worth surfacing — PM's L3d section
        then shows a default "no recurring missed themes" note.
        """
        import json as _json
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_missed_lessons: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        # RC4 (2026-07-16): value_entry_missed IS a real, actionable miss —
        # it's the category evening uses for "we identified the entry and
        # didn't take it" (SNDK was flagged 16×, ORCL 7×, and PM never saw
        # any of it because this set filtered them out). Only the two
        # "not-really-a-miss" categories stay excluded.
        real_miss_cats = {
            "trend_timing_miss", "theme_blindspot", "fundamentals_mispricing",
            "value_entry_missed",
        }
        theme_dates: dict[str, set[str]] = {}
        theme_symbols: dict[str, list[str]] = {}
        theme_lessons: dict[str, str] = {}  # most recent lesson text per theme
        for row in rows[:lookback_days]:
            row_date = row.get("date") or ""
            raw = row.get("missed_opportunities_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3d aggregates 14d of missed themes for PM. A single
                # corrupt insights row used to vanish silently from PM's
                # view; surfaces it so a recurring DB corruption pattern
                # is visible in logs instead of the layer just looking
                # "empty" some days.
                logger.warning(
                    "recent_missed_lessons: JSON parse failed for "
                    "insights row %s: %s",
                    row_date or "?", e,
                )
                continue
            if not isinstance(items, list):
                continue
            for m in items:
                if not isinstance(m, dict):
                    continue
                cat = m.get("miss_category")
                if cat not in real_miss_cats:
                    continue
                theme = (m.get("theme_if_any") or "").strip()
                sym = (m.get("symbol") or "").strip().upper()
                # DUAL grouping keys. RC4 (2026-07-16): theme_if_any is LLM
                # free text that almost never repeats verbatim (45 distinct
                # themes, 0 recurring in the audit window) — keyed ONLY by
                # theme, a symbol missed 16 times (SNDK) diluted into 16
                # one-off "themes" and PM was shown "(no recurring missed
                # themes)" every run. Symbol-keyed counting fixes that;
                # theme-keyed counting is KEPT because cross-symbol theme
                # recurrence (VST + OKLO both "nuclear/power") is a real,
                # distinct signal a symbol key can't see.
                keys = set()
                if sym:
                    keys.add(f"sym:{sym}")
                if theme:
                    keys.add(theme)
                if not keys:
                    continue
                for key in keys:
                    theme_dates.setdefault(key, set()).add(row_date)
                    theme_symbols.setdefault(key, []).append(sym)
                    # Rows are newest-first; first lesson seen is freshest.
                    if key not in theme_lessons:
                        lesson = (m.get("lesson") or "").strip()
                        if lesson:
                            theme_lessons[key] = lesson[:200]
        # Keep themes seen in ≥ 2 distinct EPISODES (audit round 2): the
        # missed-ops digest uses a rolling 5-session window, so one big
        # single-day move re-emits the same miss on ~5 consecutive evenings —
        # "≥2 distinct dates" was auto-satisfied by every one-off spike.
        # Dates within 5 days of the previous date collapse into one episode.
        def _episodes(dates: set[str]) -> int:
            from datetime import date as _d
            parsed = sorted(
                _d.fromisoformat(x) for x in dates
                if isinstance(x, str) and len(x) >= 10
            ) if dates else []
            if not parsed:
                return 0
            n = 1
            for a, b in zip(parsed, parsed[1:]):
                if (b - a).days > 5:
                    n += 1
            return n

        # Recurring = ≥2 separated episodes OR ≥2 distinct symbols. The
        # symbol arm keeps the genuine cross-symbol theme case (VST + OKLO
        # both flagged "nuclear/power" on adjacent days = one market episode
        # but a REAL breadth signal), which pure episode-counting would drop.
        recurring = [
            (k, max(_episodes(theme_dates[k]),
                    len({x for x in theme_symbols.get(k, []) if x})))
            for k in theme_dates
            if (_episodes(theme_dates[k]) >= 2
                or len({x for x in theme_symbols.get(k, []) if x}) >= 2)
        ]
        if not recurring:
            return ""
        # Sort by occurrence count desc, then key alpha for determinism.
        recurring.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for key, n_days in recurring[:5]:
            syms = theme_symbols.get(key, [])
            uniq = sorted(set(syms))
            sym_tally = ", ".join(
                f"{s}×{syms.count(s)}" if syms.count(s) > 1 else s
                for s in uniq[:6]
            )
            lesson = theme_lessons.get(key, "")
            label = key[4:] if key.startswith("sym:") else key
            line = f"- {label}: {n_days} days (symbols: {sym_tally})"
            if lesson:
                line += f' — latest lesson: "{lesson}"'
            lines.append(line)
        return "\n".join(lines)

    def _persist_evening_replay_inputs(
        self,
        *,
        date_iso: str,
        run_id: str,
        positions,
        macro_summary: dict,
        total_value: float,
        daily_pnl: float,
        daily_return_pct: float,
        today_trades: list,
        prior_outlook,
        recent_sells: list,
        recent_buys: list,
        news_intel,
        earnings_analyses: list,
        weekly_narrative: str,
        active_state_changes: str,
        outlook_calibration: dict,
        missed_ops_snapshots: list,
        thesis_health_context: dict,
        root_dir: str = "data/evening_replays",
    ) -> Path:
        """Freeze the full evening-analyst input set as JSON so a candidate
        prompt can be re-scored on the same inputs weeks later.

        Pydantic objects (Position, NewsIntelligenceReport, MissedOpportunity
        Snapshot) are serialized via model_dump; the replay script reverses
        it. Plain dicts/strings pass through untouched. Writes atomically to
        data/evening_replays/YYYY-MM-DD.json. Caller treats the whole call
        as best-effort — a disk full or permission issue on the replay dir
        should NOT break the live evening run.
        """
        from pathlib import Path as _Path
        import json as _json
        import os as _os

        def _dump(obj):
            """Recursively convert Pydantic → dict; leave plain JSON types."""
            if obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            if isinstance(obj, list):
                return [_dump(x) for x in obj]
            if isinstance(obj, tuple):
                return [_dump(x) for x in obj]
            if isinstance(obj, dict):
                return {str(k): _dump(v) for k, v in obj.items()}
            # Fall-through: stringify — better than crashing the persist.
            return str(obj)

        payload = {
            "schema_version": 1,
            "date": date_iso,
            "run_id": run_id,
            "kwargs": {
                "positions": [_dump(p) for p in (positions or [])],
                "macro_summary": _dump(macro_summary),
                "total_value": total_value,
                "daily_pnl": daily_pnl,
                "daily_return_pct": daily_return_pct,
                "today_trades": _dump(today_trades),
                "prior_outlook": _dump(prior_outlook),
                "recent_sells": _dump(recent_sells),
                "recent_buys": _dump(recent_buys),
                "news_intel": _dump(news_intel),
                "earnings_analyses": _dump(earnings_analyses),
                "weekly_narrative": weekly_narrative,
                "active_state_changes": active_state_changes,
                "outlook_calibration": _dump(outlook_calibration),
                "missed_ops_snapshots": [_dump(s) for s in (missed_ops_snapshots or [])],
                "thesis_health_context": _dump(thesis_health_context),
            },
        }

        out_dir = _Path(root_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_iso}.json"
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, indent=2, ensure_ascii=False))
        _os.replace(str(tmp), str(out_path))
        logger.info("Evening replay inputs frozen → %s", out_path)
        return out_path

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

    def _build_recent_loss_pits(self, lookback_days: int = 14) -> str:
        """PM L3f memory: repeat failure modes from losing BUYs.

        Reads `insights.buy_grades_json` for the last N days, pulls entries
        with `grade="wrong"` and a non-null `loss_root_cause`, groups by
        cause, keeps causes occurring ≥ 2 times. Output is prose PM renders
        directly — lets it see "greed_top_chasing × 3 over 14 days"
        BEFORE deciding today's sizing, not after another wrong entry.

        Empty string when no repeat pattern — PM's L3f section then shows
        a default "no recurring pits" note.
        """
        import json as _json
        try:
            rows = self.db.get_recent_insights(limit=lookback_days + 5)
        except Exception as e:
            logger.warning("recent_loss_pits: insights fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        cause_symbols: dict[str, list[str]] = {}
        cause_move: dict[str, list[float]] = {}
        cause_refs: dict[str, list[str]] = {}
        for row in rows[:lookback_days]:
            raw = row.get("buy_grades_json")
            if not raw:
                continue
            try:
                items = _json.loads(raw)
            except (TypeError, ValueError) as e:
                # L3f aggregates 14d of loss-root-cause patterns. Same
                # silent-drop rationale as L3d above.
                logger.warning(
                    "recent_loss_pits: JSON parse failed for insights "
                    "row %s: %s",
                    (row.get("date") or "?"), e,
                )
                continue
            if not isinstance(items, list):
                continue
            for g in items:
                if not isinstance(g, dict):
                    continue
                if g.get("grade") != "wrong":
                    continue
                cause = (g.get("loss_root_cause") or "").strip()
                if not cause:
                    continue
                sym = (g.get("symbol") or "").strip().upper()
                move = g.get("pct_move_since_buy")
                ref = (g.get("missed_warning_ref") or "").strip()
                if sym:
                    cause_symbols.setdefault(cause, []).append(sym)
                if isinstance(move, (int, float)):
                    cause_move.setdefault(cause, []).append(float(move))
                if ref:
                    cause_refs.setdefault(cause, []).append(ref[:100])
        repeats = [(c, len(cause_symbols.get(c, []))) for c in cause_symbols
                   if len(cause_symbols.get(c, [])) >= 2]
        if not repeats:
            return ""
        repeats.sort(key=lambda x: (-x[1], x[0]))
        lines: list[str] = []
        for cause, n in repeats[:4]:
            syms = cause_symbols[cause]
            moves = cause_move.get(cause, [])
            detail_bits: list[str] = []
            for i, s in enumerate(syms[:4]):
                m = moves[i] if i < len(moves) else None
                detail_bits.append(f"{s} ({m:+.1f}%)" if m is not None else s)
            line = f"- {cause} × {n}: {', '.join(detail_bits)}"
            refs = cause_refs.get(cause, [])
            if refs and cause == "macro_warning_ignored":
                line += f' — ignored: "{refs[0]}"'
            lines.append(line)
        return "\n".join(lines)

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

    @staticmethod
    def _actualize_trade_row(row: dict) -> dict:
        """Prefer broker-confirmed execution details when present."""
        out = dict(row)
        if out.get("fill_qty"):
            out["qty"] = float(out["fill_qty"])
        if out.get("fill_price"):
            out["price"] = float(out["fill_price"])
        return out

    @staticmethod
    def _build_macro_tech_alignment(
        macro_analysis: dict | None,
        analyses: list,
    ) -> str:
        """Advisory: does Macro's equity outlook match TA's rating distribution?

        Macro says 'bullish' but TA's ratings are majority bearish → market
        action is diverging from the macro call. That's a signal for PM to
        weight today's TA signals more carefully (market is often right
        about regime flips before FRED data catches up).

        Returns empty string when no divergence, or there's not enough data.
        """
        if not macro_analysis or not analyses:
            return ""
        # macro_analysis is MacroAnalysis (Pydantic) post-Phase-4-#7; dict path
        # still supported for defensive compatibility with legacy callers.
        if hasattr(macro_analysis, "equity_outlook"):
            outlook = (macro_analysis.equity_outlook or "").lower()
        else:
            outlook = (macro_analysis.get("equity_outlook") or "").lower()
        if outlook not in ("bullish", "bearish"):
            return ""
        bullish = sum(1 for a in analyses if a.rating in ("buy", "strong_buy"))
        bearish = sum(1 for a in analyses if a.rating in ("sell", "strong_sell"))
        total = len(analyses)
        if total < 5:
            return ""  # too small a sample to read a tape
        if outlook == "bullish" and bearish > bullish:
            return (
                f"DIVERGENCE: Macro `equity_outlook=bullish` but TA has more bearish "
                f"ratings ({bearish}) than bullish ({bullish}) across {total} symbols. "
                f"Market action may be leading the data — tread carefully on new BUYs "
                f"and respect TA's cautious signals."
            )
        if outlook == "bearish" and bullish > bearish:
            return (
                f"DIVERGENCE: Macro `equity_outlook=bearish` but TA has more bullish "
                f"ratings ({bullish}) than bearish ({bearish}) across {total} symbols. "
                f"Market may be pricing a turnaround before Macro data confirms — "
                f"don't ignore high-R/R long setups just because Macro is cautious."
            )
        return ""

    def _ensure_correlation_matrix(self, ctx, positions) -> dict:
        """Build the run's correlation matrix once, memoized on `ctx`.

        It used to be built inside `RiskStage`, which runs AFTER the Portfolio
        Manager has already chosen — so the PM's prompt could tell it to "avoid
        stacking highly correlated positions" while the only correlation data
        in the system was computed too late to inform that choice (audit §1.2).
        Building it here, from the DecisionStage side, lets PM see the clusters
        BEFORE it decides, and RiskStage reuses the same matrix rather than
        paying for a second one — the deterministic cluster check must judge
        PM against the numbers PM was actually shown.
        """
        cached = getattr(ctx, "correlation_matrix", None)
        if cached:
            return cached
        try:
            from src.data.correlation import build_correlation_matrix
            # THE CORRELATION WINDOW (board item 148), recorded honestly.
            # The bars that feed this matrix span `trading.lookback_days`
            # (deployed 1800 ≈ 5 trading years, config/settings.yaml) — the
            # SAME history fetched for MA200 and every other indicator, reused
            # here rather than chosen for correlation. It has NO correlation-
            # specific derivation: settings.yaml records the 320→1800 raise as
            # "purely for structure" (deterministic support/resistance), and
            # `build_correlation_matrix` needs only 20 overlapping daily returns
            # (`df.corr(min_periods=20)`) over pairwise-complete observations, so
            # the extra ~1,780 bars add older returns that may straddle regime
            # changes rather than sharpen a cluster estimate. The window moved
            # 120d → 5y silently on the switch to `trading.lookback_days`; this
            # comment is the reason that was never recorded — it is INHERITED
            # from the structural-level fetch, not justified for clustering.
            # It is not a ledgered number: `trading.lookback_days` carries no
            # numeric default (`Field(ge=1)` in src/config.py), so it is not a
            # definition site the number-ledger scanner can attach an entry to;
            # the 0.7 cutoff that used to sit beside it is GONE (item 186,
            # 2026-09-30): clusters are now read from the correlation
            # geometry itself, so the window is the only unjustified input
            # left on this path.
            pool_bars = dict(ctx.symbols_bars)
            for p in positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = self.market.get_ohlcv(
                        p.symbol, self.config.trading.lookback_days,
                    ) or []
            matrix = build_correlation_matrix(pool_bars) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
            matrix = {}
        ctx.correlation_matrix = matrix
        return matrix

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

    @staticmethod
    def _log_conviction_outcome_for_operator(stats: dict) -> None:
        """Log the FULL by_conviction / by_allocated_risk breakdown for a
        human operator reading logs — including every bucket below
        `_CONVICTION_OUTCOME_MIN_N`, which `_build_calibration_note` never
        puts in front of an agent (see the "MOST IMPORTANT CONSTRAINT" note
        at its call site). This is the ONLY place that count is surfaced at
        all: recorded, not silently dropped, per spec §7.2 — "that must be
        discovered from data, not assumed" cuts both ways: assumed-absent
        is as wrong as assumed-present.
        """
        try:
            parts = []
            for grouping_key in ("by_conviction", "by_allocated_risk"):
                grouping = stats.get(grouping_key) or {}
                bucket_strs = []
                for label, s in grouping.items():
                    if not s:
                        continue
                    if s.get("insufficient_data"):
                        bucket_strs.append(f"{label}: n={s.get('n', 0)} (below floor)")
                    else:
                        bucket_strs.append(
                            f"{label}: n={s.get('n')} win={s.get('win_rate_pct')}% "
                            f"avg={s.get('avg_return_pct')}%"
                        )
                if bucket_strs:
                    parts.append(f"{grouping_key}=[{'; '.join(bucket_strs)}]")
            if not parts:
                return
            logger.info(
                "Conviction/risk-outcome calibration (OPERATOR-ONLY — never "
                "sent to any agent prompt below the sample floor): %s | "
                "conviction_unknown_n=%s allocated_risk_unknown_n=%s",
                " ".join(parts),
                stats.get("conviction_unknown_n"),
                stats.get("allocated_risk_unknown_n"),
            )
        except Exception as e:  # noqa: BLE001 — logging must never break calibration
            logger.warning("conviction_outcome operator log failed: %s", e)

    def _build_calibration_note(self, lookback_days: int = 45) -> str:
        """Render PM's own hit rate + avg return on closed BUYs in the window.

        L4 calibration memory — the answer to 'has my conviction actually paid
        off recently?'. Without this PM keeps sizing confidence on today's
        alignment score alone, even if that score has been losing lately.
        """
        try:
            stats = self.db.compute_trade_calibration(lookback_days=lookback_days)
        except Exception as e:
            logger.warning("calibration_note: stats failed: %s", e)
            return ""
        if not isinstance(stats, dict) or not stats:
            return ""
        # Conviction ledger (spec §7.2) — operator-only surface. Logged on
        # EVERY call regardless of the floor below, deliberately separate
        # from the prompt text being built: this is how a human operator
        # sees "n=8, split 4/3/1, too few to conclude anything" WITHOUT it
        # ever reaching an agent. Never gate this log on the floor — the
        # whole point is that the operator sees the sub-floor count too.
        self._log_conviction_outcome_for_operator(stats)
        try:
            if stats.get("n", 0) < 3:
                return ""
        except TypeError:
            return ""
        lines = [
            f"- Overall (last {stats.get('lookback_days', lookback_days)}d): "
            f"{stats['n']} closed BUYs, win rate {stats['win_rate_pct']:.0f}%, "
            f"avg return {stats['avg_return_pct']:+.2f}%, avg hold {stats['avg_hold_days']:.1f}d"
        ]
        by_size = stats.get("by_size") or {}
        for label, s in by_size.items():
            if not s or s.get("n", 0) == 0:
                continue
            lines.append(
                f"  - {label}: {s['n']} trades, win {s['win_rate_pct']:.0f}%, "
                f"avg {s['avg_return_pct']:+.2f}%, hold {s['avg_hold_days']:.1f}d"
            )
        # Conviction ledger (spec §7.2) — THE MOST IMPORTANT CONSTRAINT on
        # this whole feature: a bucket below `_CONVICTION_OUTCOME_MIN_N`
        # (db.py) is `_gated_bucket_stats`-shaped ({"n", "insufficient_data":
        # True, "message"}) and is skipped here ENTIRELY — no header, no
        # line, nothing appended to `lines` — never a "too few trades" line
        # either, because even that much would put the bucket's existence
        # and its raw direction in front of the model. Only a bucket that
        # has cleared the floor (`insufficient_data` False) ever reaches
        # this prompt text. With production at n=8 total (2026-08-30),
        # EVERY bucket in both groupings is below floor, so today this
        # appends nothing at all — that is the correct, intended behaviour,
        # not a bug to "fix" by lowering the floor.
        for grouping_key, section_label in (
            ("by_conviction", "By conviction"),
            ("by_allocated_risk", "By allocated risk"),
        ):
            grouping = stats.get(grouping_key) or {}
            qualifying = [
                (label, s) for label, s in grouping.items()
                if s and not s.get("insufficient_data", True) and s.get("n", 0) > 0
            ]
            if not qualifying:
                continue
            lines.append(f"  {section_label} (established sample):")
            for label, s in qualifying:
                lines.append(
                    f"    - {label}: {s['n']} trades, win {s['win_rate_pct']:.0f}%, "
                    f"avg {s['avg_return_pct']:+.2f}%, hold {s['avg_hold_days']:.1f}d"
                )
        return "\n".join(lines)

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

    def _build_own_recent_decisions(self, limit: int = 3) -> str:
        """Pull last N position_reviewer sessions from agent_logs.

        Anti-flip-flop memory: shows the reviewer its own previous 3 sessions'
        actions per symbol so it can't silently reverse itself within hours
        without a named trigger. Complement to PM's `_build_pm_recent_decisions`.
        """
        try:
            # No before_date cutoff (audit round 2): the 15:30 close session
            # must see the 13:00 midday row — this anti-flip-flop memory says
            # "don't reverse yourself WITHIN HOURS", and the ET-midnight
            # cutoff excluded exactly those rows. The current session's own
            # row is inserted AFTER this builder runs, so no self-read.
            rows = self.db.get_recent_agent_outputs(
                agent_name="position_reviewer", limit=limit,
            )
        except Exception as e:
            logger.warning("own_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines: list[str] = []
        for row in reversed(rows):  # oldest → newest
            ts = (row.get("timestamp") or "")[:16]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                continue
            action_bits = []
            for a in actions:
                if not isinstance(a, dict):
                    continue
                sym = a.get("symbol", "?")
                act = a.get("action", "?")
                if act == "HOLD":
                    continue  # only surface actionable past decisions
                action_bits.append(f"{sym}:{act}")
            if action_bits:
                lines.append(f"- {ts}: {', '.join(action_bits[:8])}")
        return "\n".join(lines)
