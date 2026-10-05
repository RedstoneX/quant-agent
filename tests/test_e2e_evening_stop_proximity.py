"""The hermetic EVENING session's "close to its stop" finding.

The evening audit tells the owner which holdings could reach their stop in
one ordinary session. tests/test_e2e_evening_existing_book.py proves
coverage gaps, P&L and the report row; nothing end to end proves THIS
finding. Here the real evening session runs on a book of one long with a
stop resting at the broker, and the price is placed relative to that stop:

  tight    the last price is 0.3 above the stop, far under the symbol's own
           ordinary daily range: reported as "near", persisted;
  roomy    the price is ~8 above the stop: nothing is reported;
  through  the price is below a stop that never filled: reported as its
           own state, "through", never merged into "near".

Every expectation derives from the inputs (price, stop), not desk output.
"""
from __future__ import annotations

import json

import pytest

from tests.e2e_held_book_support import run_held_book
from tests.test_e2e_close_existing_book import INITIAL_STOP, SYMBOL, _bars
from tests.test_e2e_evening_existing_book import HOUR, _analyst_says
from tests.test_e2e_close_existing_book import _news_says_nothing


def _evening(tmp_path, monkeypatch, price):
    answers = {"evening": _analyst_says(), "news": _news_says_nothing()}
    return run_held_book(tmp_path, monkeypatch, session="evening", hour=HOUR,
                         bars=_bars(end=price), answers=answers)


def _rows(result):
    return [r for r in result["stop_proximity"] if r["symbol"] == SYMBOL]


def test_a_tight_stop_is_reported_near_and_persisted(tmp_path, monkeypatch):
    price = INITIAL_STOP + 0.3
    result, _trace, trading, attempts, pipeline = _evening(
        tmp_path, monkeypatch, price)
    assert attempts == [], attempts
    rows = _rows(result)
    assert len(rows) == 1, result["stop_proximity"]
    assert rows[0]["status"] == "near", rows[0]
    assert rows[0]["stop"] == INITIAL_STOP
    assert rows[0]["gap"] == pytest.approx(0.3)
    assert rows[0]["gap"] < rows[0]["atr"]
    stored = pipeline.db.get_evening_report()
    assert stored is not None and SYMBOL in json.dumps(stored), (
        "the near-stop finding did not reach the stored evening report")
    assert trading.submitted == [] and trading.cancelled == []


def test_a_roomy_stop_is_not_reported(tmp_path, monkeypatch):
    result, *_ = _evening(tmp_path, monkeypatch, INITIAL_STOP + 8.0)
    assert _rows(result) == [], result["stop_proximity"]


def test_a_price_through_an_unfilled_stop_is_its_own_state(
    tmp_path, monkeypatch,
):
    price = INITIAL_STOP - 1.0
    result, *_ = _evening(tmp_path, monkeypatch, price)
    rows = _rows(result)
    assert len(rows) == 1 and rows[0]["status"] == "through", rows
    assert rows[0]["through"] == pytest.approx(1.0)
