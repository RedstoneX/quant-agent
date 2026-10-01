"""Prompt-fact family: how recent trades turned out: the sells and buys up for grading, the post-exit reality check, missed lessons, loss pits, outlook calibration and the evening-replay inputs.

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210: one of
the six fact families split out of `PromptFacts`. Method bodies are the
former `PromptFacts` bodies byte for byte; this class takes ONLY the
5 collaborators those bodies read. Nothing here may import
`src.pipeline` or `src.pipeline_prompt_facts`.
"""

import json as _json
from pathlib import Path
from src.trading_calendar import et_today, session_date_key
from src.prompt_facts_ports import _ABSENT, bind_ports, logger


class GradingFacts:
    """How recent trades turned out: the sells and buys up for grading, the post-exit reality check, missed lessons, loss pits, outlook calibration and the evening-replay inputs."""

    def __init__(
        self,
        *,
        db=_ABSENT,
        broker=_ABSENT,
        market=_ABSENT,
        sweeper=_ABSENT,
        exit_audit_actions=_ABSENT,
    ) -> None:
        bind_ports(self, {
            "db": db,
            "broker": broker,
            "market": market,
            "_sweeper": sweeper,
            "_EXIT_AUDIT_ACTIONS": exit_audit_actions,
        })

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
