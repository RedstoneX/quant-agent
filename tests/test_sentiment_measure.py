"""Tests for src.sentiment_measure (board item 125).

The check must be able to show an earnings sentiment verdict was WRONG, from
the verdicts already on disk and the bars already fetched, without inventing
any trade threshold. Covers:
  - score_direction: bullish/bearish confirmed & contradicted, flat move,
    neutral, non-positive prices (all sign-based, no band)
  - forward_return: entry = first bar on/after filing, to-latest vs fixed
    horizon, unresolved window, filing newer than every bar
  - iter_stored_verdicts: round-trips real analysis files off disk
  - measure / build_report: aggregates hit-rate and surfaces a contradicted
    (wrong) verdict end-to-end
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from src.sentiment_measure import (
    build_report,
    forward_return,
    iter_stored_verdicts,
    measure,
    score_direction,
)


@dataclass
class _Bar:
    date: date
    close: float


def _series(start: date, closes: list[float]) -> list[_Bar]:
    return [_Bar(date=start + timedelta(days=i), close=c) for i, c in enumerate(closes)]


# ---------------------------------------------------------------------------
# score_direction — sign only, no band
# ---------------------------------------------------------------------------

def test_bullish_up_is_correct():
    assert score_direction("bullish", 100.0, 110.0) is True


def test_bullish_down_is_wrong():
    assert score_direction("bullish", 100.0, 90.0) is False


def test_bearish_down_is_correct():
    assert score_direction("bearish", 100.0, 90.0) is True


def test_bearish_up_is_wrong():
    assert score_direction("BEARISH", 100.0, 101.0) is False  # case-insensitive


def test_flat_move_is_unscorable():
    assert score_direction("bullish", 100.0, 100.0) is None


def test_neutral_is_never_scored():
    assert score_direction("neutral", 100.0, 130.0) is None
    assert score_direction("neutral", 100.0, 70.0) is None


def test_unrecognised_and_bad_prices_are_none():
    assert score_direction("", 100.0, 110.0) is None
    assert score_direction("bullish", 0.0, 110.0) is None
    assert score_direction("bullish", 100.0, -1.0) is None


# ---------------------------------------------------------------------------
# forward_return
# ---------------------------------------------------------------------------

def test_forward_return_to_latest_close():
    bars = _series(date(2026, 1, 1), [100.0, 101.0, 105.0])
    fr = forward_return(bars, "2026-01-01", horizon_sessions=None)
    assert fr is not None
    assert fr["entry_close"] == 100.0
    assert fr["exit_close"] == 105.0
    assert fr["forward_return_pct"] == 5.0
    assert fr["sessions_forward"] == 2


def test_entry_is_first_bar_on_or_after_filing():
    # Filing lands between bar[0] and bar[1]; entry must be bar[1].
    bars = [
        _Bar(date(2026, 1, 1), 100.0),
        _Bar(date(2026, 1, 5), 200.0),
        _Bar(date(2026, 1, 6), 210.0),
    ]
    fr = forward_return(bars, "2026-01-03", horizon_sessions=None)
    assert fr is not None
    assert fr["entry_date"] == "2026-01-05"
    assert fr["entry_close"] == 200.0
    assert fr["exit_close"] == 210.0


def test_fixed_horizon_picks_nth_session():
    bars = _series(date(2026, 1, 1), [100.0, 101.0, 102.0, 130.0, 140.0])
    fr = forward_return(bars, "2026-01-01", horizon_sessions=3)
    assert fr is not None
    assert fr["exit_close"] == 130.0
    assert fr["sessions_forward"] == 3


def test_unresolved_horizon_returns_none():
    bars = _series(date(2026, 1, 1), [100.0, 101.0])
    assert forward_return(bars, "2026-01-01", horizon_sessions=5) is None


def test_filing_newer_than_all_bars_returns_none():
    bars = _series(date(2026, 1, 1), [100.0, 101.0])
    assert forward_return(bars, "2026-06-01", horizon_sessions=None) is None


def test_empty_bars_returns_none():
    assert forward_return([], "2026-01-01") is None


# ---------------------------------------------------------------------------
# iter_stored_verdicts — real disk layout
# ---------------------------------------------------------------------------

def _write_analysis(root: Path, symbol: str, form: str, filing_date: str,
                    sentiment: str, conviction: str) -> None:
    d = root / symbol
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "form_type": form,
        "filing_date": filing_date,
        "investment_implications": {
            "sentiment": sentiment,
            "conviction": conviction,
            "key_thesis": "…",
        },
    }
    md = (
        f"# {symbol} {form} Analysis ({filing_date})\n\n"
        f"## Full Analysis\n\n```json\n{json.dumps(payload, indent=2)}\n```\n"
    )
    (d / f"analysis_{form}_{filing_date}.md").write_text(md)


def test_iter_stored_verdicts_reads_all_dated_files(tmp_path):
    _write_analysis(tmp_path, "AAPL", "10-Q", "2026-01-05", "bullish", "high")
    _write_analysis(tmp_path, "AAPL", "10-K", "2025-11-01", "neutral", "medium")
    _write_analysis(tmp_path, "XYZ", "10-Q", "2026-02-01", "bearish", "low")

    verdicts = iter_stored_verdicts(str(tmp_path))
    assert len(verdicts) == 3
    by_key = {(v["symbol"], v["form_type"]): v for v in verdicts}
    assert by_key[("AAPL", "10-Q")]["sentiment"] == "bullish"
    assert by_key[("XYZ", "10-Q")]["conviction"] == "low"


def test_iter_stored_verdicts_missing_dir_is_empty():
    assert iter_stored_verdicts("/no/such/earnings/dir") == []


# ---------------------------------------------------------------------------
# measure / build_report — end to end, surfaces a WRONG verdict
# ---------------------------------------------------------------------------

def test_measure_flags_a_contradicted_verdict():
    verdicts = [
        {"symbol": "UP", "form_type": "10-Q", "filing_date": "2026-01-01",
         "sentiment": "bullish", "conviction": "high"},
        {"symbol": "DOWN", "form_type": "10-Q", "filing_date": "2026-01-01",
         "sentiment": "bullish", "conviction": "high"},
        {"symbol": "NEU", "form_type": "10-Q", "filing_date": "2026-01-01",
         "sentiment": "neutral", "conviction": "low"},
    ]
    bars = {
        "UP": _series(date(2026, 1, 1), [100.0, 120.0]),    # bullish + up = right
        "DOWN": _series(date(2026, 1, 1), [100.0, 80.0]),   # bullish + down = WRONG
        "NEU": _series(date(2026, 1, 1), [100.0, 150.0]),   # neutral = unscored
    }
    report = measure(verdicts, bars)
    assert report["n_verdicts"] == 3
    assert report["n_resolved"] == 2          # neutral not scored
    assert report["n_wrong"] == 1             # DOWN was contradicted

    recs = {r["symbol"]: r for r in report["records"]}
    assert recs["DOWN"]["correct"] is False
    assert recs["DOWN"]["forward_return_pct"] == -20.0
    assert recs["UP"]["correct"] is True
    assert recs["NEU"]["correct"] is None

    assert report["by_sentiment"]["bullish"]["hit_rate_pct"] == 50.0
    assert report["by_conviction"]["high"]["wrong"] == 1


def test_build_report_reads_disk_and_uses_injected_bars(tmp_path):
    _write_analysis(tmp_path, "DOWN", "10-Q", "2026-01-01", "bullish", "high")

    def _fetch(symbol: str):
        assert symbol == "DOWN"
        return _series(date(2026, 1, 1), [100.0, 70.0])

    report = build_report(str(tmp_path), _fetch, horizon_sessions=None)
    assert report["n_wrong"] == 1
    assert report["records"][0]["correct"] is False


def test_build_report_survives_a_failing_symbol(tmp_path):
    _write_analysis(tmp_path, "BAD", "10-Q", "2026-01-01", "bullish", "high")

    def _fetch(symbol: str):
        raise RuntimeError("data provider down")

    report = build_report(str(tmp_path), _fetch)
    assert report["n_verdicts"] == 1
    assert report["n_resolved"] == 0          # unresolved, not crashed
    assert report["records"][0]["correct"] is None
