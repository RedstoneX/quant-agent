"""src.prompt_facts.outcome_review -- Post-exit reality, loss pits, the calibration note and recent performance: what happened after the desk acted.

Bodies moved verbatim (AST-identical) from src/pipeline_prompt_facts_review.py, the
third prompt-facts instalment. Every collaborator is an explicit keyword-only
constructor argument; nothing here imports src.pipeline. Read-only on the book.
"""
from __future__ import annotations

import logging
import json as _json
from src.risk.rules import peak_to_trough_pct

#: The moved code logged under `src.pipeline_prompt_facts_review` before the move
#: and still does; binding the name keeps log records byte-identical.
logger = logging.getLogger("src.pipeline_prompt_facts_review")


class OutcomeReview:
    """Post-exit reality, loss pits, the calibration note and recent performance: what happened after the desk acted."""

    def __init__(
        self, *,
        exit_audit_actions,
        log_conviction_outcome_for_operator,
        sweeper,
        broker,
        db,
    ) -> None:
        self._EXIT_AUDIT_ACTIONS = exit_audit_actions
        self._log_conviction_outcome_for_operator = log_conviction_outcome_for_operator
        self._sweeper = sweeper
        self.broker = broker
        self.db = db

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
