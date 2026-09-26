"""Board item 125 — measure whether the earnings seat's sentiment verdicts
are any good, against the price that actually followed.

WHAT THIS CLOSES. The earnings analyst emits a `sentiment`
(bullish/bearish/neutral) + `conviction` per filing, derived from a
five-field `reasoning_chain` and written to disk
(`data/earnings/{SYMBOL}/analysis_{form}_{date}.md`). That derivation is
audited for INTERNAL consistency — "show the arithmetic" — but nothing ever
checked the enum against reality: a durably bullish call on a stock that then
fell was never counted as wrong. This module is that check.

DELIBERATELY NOT A TRADE GATE. It scores verdicts after the fact; it invents
no threshold that sizes or gates anything, and it does not touch the
five-field derivation (item 125's own guard). A bullish verdict is scored
WRONG only when the realized return is negative and a bearish one only when it
is positive — the sign of the move, no band, no tuned constant. Neutral
verdicts carry no directional claim, so they are recorded but not scored
right/wrong.

FED BY REAL, ALREADY-PAID SESSIONS. The verdicts are read from the on-disk
earnings store the desk already writes every run; the realized outcome is the
daily bars the desk already fetches. No new benchmark, no test environment.
The forward return is recomputed from verdict + bars each time (it is
recoverable, so it is not separately persisted — the verdict already is).

Pure logic plus one disk read; the market-data fetch is injected so the
measurement is testable without a broker. Nothing here writes to the broker,
sizes a position, or feeds sizing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


# Directional verdicts this module can falsify against a realized move.
# `neutral` (and anything unrecognised) makes no directional claim, so it is
# recorded but never scored right or wrong.
_DIRECTIONAL = {"bullish", "bearish"}


def score_direction(
    sentiment: str,
    entry_close: float,
    exit_close: float,
) -> Optional[bool]:
    """Was the directional verdict borne out by the realized move?

    Returns:
      * True  — the move went the way the verdict said (bullish & up, or
                bearish & down).
      * False — the move went AGAINST the verdict (bullish & down, or
                bearish & up). This is the "verdict was wrong" signal item
                125 asks for.
      * None  — not falsifiable: a neutral / unrecognised verdict, a
                non-positive price, or a flat move (exactly zero return, so
                neither confirmed nor contradicted).

    Sign only — no neutral band and no tuned constant, so no number here can
    ever be read as a trade threshold.
    """
    s = (sentiment or "").strip().lower()
    if s not in _DIRECTIONAL:
        return None
    if entry_close <= 0 or exit_close <= 0:
        return None
    if exit_close == entry_close:
        return None
    went_up = exit_close > entry_close
    if s == "bullish":
        return went_up
    return not went_up  # bearish


def forward_return(
    bars: Sequence,
    filing_date: str,
    horizon_sessions: Optional[int] = None,
) -> Optional[dict]:
    """Realized forward move for one verdict from a series of daily bars.

    `bars` is any sequence of objects carrying `.date` (a `datetime.date`)
    and `.close` (float) — the `OHLCV` shape the broker and market-data
    provider both return.

    Entry is the first bar on OR after `filing_date` (the first session the
    market could act on the filing). Exit is:
      * the last available bar, when `horizon_sessions` is None — a
        parameter-free "return since the filing to now", the same measure
        `_build_recent_buys` already uses for BUYs; or
      * the bar `horizon_sessions` trading sessions after entry, when a
        caller supplies an explicit measurement window.

    `horizon_sessions` is a MEASUREMENT window chosen by the caller, never a
    trade parameter, and has no default baked into any trading path.

    Returns {entry_date, exit_date, entry_close, exit_close,
    forward_return_pct, sessions_forward} or None when the series cannot
    resolve an entry and a distinct exit.
    """
    if not bars:
        return None
    try:
        target = _parse_date(filing_date)
    except ValueError:
        return None
    if target is None:
        return None

    ordered = sorted(bars, key=lambda b: b.date)
    entry_idx = None
    for i, b in enumerate(ordered):
        if b.date >= target:
            entry_idx = i
            break
    if entry_idx is None:
        return None  # filing is newer than every bar we have

    if horizon_sessions is None:
        exit_idx = len(ordered) - 1
    else:
        if horizon_sessions <= 0:
            return None
        exit_idx = entry_idx + horizon_sessions
        if exit_idx >= len(ordered):
            return None  # window has not fully resolved yet

    if exit_idx <= entry_idx:
        return None  # no forward window to measure

    entry = ordered[entry_idx]
    exit_ = ordered[exit_idx]
    if entry.close <= 0 or exit_.close <= 0:
        return None
    pct = (exit_.close / entry.close - 1) * 100
    return {
        "entry_date": str(entry.date),
        "exit_date": str(exit_.date),
        "entry_close": round(float(entry.close), 4),
        "exit_close": round(float(exit_.close), 4),
        "forward_return_pct": round(pct, 4),
        "sessions_forward": exit_idx - entry_idx,
    }


def iter_stored_verdicts(data_dir: str = "data/earnings") -> list[dict]:
    """Read every earnings sentiment verdict the desk has on disk.

    The manifest only points at the LATEST filing per symbol, but each
    filing's analysis is written to its own dated file that is never
    overwritten, so globbing recovers the full verdict history — exactly the
    already-paid sessions item 125 wants scored.

    Returns a list of {symbol, form_type, filing_date, sentiment,
    conviction, analysis_path}. Files that do not parse are skipped with a
    warning (they are already surfaced loudly by the deep-dive reader).
    """
    # Reuse the canonical JSON-block extractor so this cannot drift from how
    # every other reader parses the same files.
    from src.data.earnings_deep_dive import _extract_json_block

    root = Path(data_dir)
    if not root.exists():
        return []

    out: list[dict] = []
    for path in sorted(root.glob("*/analysis_*.md")):
        try:
            markdown = path.read_text()
        except OSError as exc:
            logger.warning("sentiment_measure: cannot read %s: %s", path, exc)
            continue
        data = _extract_json_block(markdown, path=path)
        if not isinstance(data, dict):
            continue
        impl = data.get("investment_implications") or {}
        symbol = (data.get("symbol") or "").strip()
        filing_date = (data.get("filing_date") or "").strip()
        if not symbol or not filing_date:
            continue
        out.append({
            "symbol": symbol,
            "form_type": (data.get("form_type") or "").strip(),
            "filing_date": filing_date,
            "sentiment": (impl.get("sentiment") or "").strip().lower(),
            "conviction": (impl.get("conviction") or "").strip().lower(),
            "analysis_path": str(path),
        })
    return out


def measure(
    verdicts: Iterable[dict],
    bars_by_symbol: dict,
    horizon_sessions: Optional[int] = None,
) -> dict:
    """Join verdicts with realized bars and score directional correctness.

    `bars_by_symbol` maps SYMBOL -> sequence of daily bars. A symbol with no
    bars, or a verdict whose forward window has not resolved, yields a
    record with `correct: None` (unresolved) rather than being dropped, so
    the report shows coverage honestly.

    Returns:
      {
        "records": [ {symbol, filing_date, form_type, sentiment,
                      conviction, forward_return_pct, correct, ...}, ... ],
        "by_sentiment": {sentiment: {resolved, correct, wrong,
                         hit_rate_pct}},
        "by_conviction": {conviction: {...}},
        "n_verdicts": int, "n_resolved": int, "n_wrong": int,
      }
    """
    records: list[dict] = []
    for v in verdicts:
        sym = v.get("symbol", "")
        bars = bars_by_symbol.get(sym) or bars_by_symbol.get(sym.upper()) or []
        rec = {
            "symbol": sym,
            "form_type": v.get("form_type", ""),
            "filing_date": v.get("filing_date", ""),
            "sentiment": v.get("sentiment", ""),
            "conviction": v.get("conviction", ""),
            "forward_return_pct": None,
            "entry_date": None,
            "exit_date": None,
            "sessions_forward": None,
            "correct": None,
        }
        fr = forward_return(bars, v.get("filing_date", ""), horizon_sessions)
        if fr is not None:
            rec.update({
                "forward_return_pct": fr["forward_return_pct"],
                "entry_date": fr["entry_date"],
                "exit_date": fr["exit_date"],
                "sessions_forward": fr["sessions_forward"],
                "correct": score_direction(
                    v.get("sentiment", ""),
                    fr["entry_close"],
                    fr["exit_close"],
                ),
            })
        records.append(rec)

    def _bucket(key: str) -> dict:
        agg: dict[str, dict] = {}
        for r in records:
            b = agg.setdefault(
                r.get(key) or "unknown",
                {"resolved": 0, "correct": 0, "wrong": 0, "hit_rate_pct": None},
            )
            if r["correct"] is True:
                b["resolved"] += 1
                b["correct"] += 1
            elif r["correct"] is False:
                b["resolved"] += 1
                b["wrong"] += 1
        for b in agg.values():
            if b["resolved"]:
                b["hit_rate_pct"] = round(100 * b["correct"] / b["resolved"], 1)
        return agg

    n_resolved = sum(1 for r in records if r["correct"] is not None)
    n_wrong = sum(1 for r in records if r["correct"] is False)
    return {
        "records": records,
        "by_sentiment": _bucket("sentiment"),
        "by_conviction": _bucket("conviction"),
        "n_verdicts": len(records),
        "n_resolved": n_resolved,
        "n_wrong": n_wrong,
        "horizon_sessions": horizon_sessions,
    }


def build_report(
    data_dir: str,
    fetch_bars: Callable[[str], Sequence],
    horizon_sessions: Optional[int] = None,
) -> dict:
    """End-to-end: read the on-disk verdicts, fetch each symbol's real bars
    via the injected `fetch_bars(symbol) -> sequence[OHLCV]`, and score them.

    `fetch_bars` is injected so tests feed synthetic bars and operators feed
    the real broker; this module never constructs a broker itself.
    """
    verdicts = iter_stored_verdicts(data_dir)
    bars_by_symbol: dict[str, Sequence] = {}
    for sym in {v["symbol"] for v in verdicts}:
        try:
            bars_by_symbol[sym] = fetch_bars(sym) or []
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not sink the report
            logger.warning("sentiment_measure: bars fetch failed for %s: %s", sym, exc)
            bars_by_symbol[sym] = []
    return measure(verdicts, bars_by_symbol, horizon_sessions)


def _parse_date(value: str):
    from datetime import date

    if not value:
        return None
    return date.fromisoformat(value.strip())
