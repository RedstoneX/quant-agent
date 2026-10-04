"""src.prompt_facts.outlook_review -- Outlook calibration and missed lessons: what the seats expected against what the tape did.

Bodies moved verbatim (AST-identical) from src/pipeline_prompt_facts_review.py, the
third prompt-facts instalment. Every collaborator is an explicit keyword-only
constructor argument; nothing here imports src.pipeline. Read-only on the book.
"""
from __future__ import annotations

import logging
import json as _json

#: The moved code logged under `src.pipeline_prompt_facts_review` before the move
#: and still does; binding the name keeps log records byte-identical.
logger = logging.getLogger("src.pipeline_prompt_facts_review")


class OutlookReview:
    """Outlook calibration and missed lessons: what the seats expected against what the tape did."""

    def __init__(
        self, *,
        db,
    ) -> None:
        self.db = db

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
