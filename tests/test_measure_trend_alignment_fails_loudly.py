"""The alignment measurement must never report success having measured nothing.

The first run of this measurement printed a full results table of zeros with
"symbols: 0" and its figures were relayed to the owner as fact. The reproduced
cause (yfinance 1.6.0, 2026-09-30) is that `yf.download` of several tickers
returns columns keyed (price field, ticker), so slicing that frame by ticker
raises `KeyError` and a swallowed failure leaves every symbol with no bars.

These tests hold the guards that make that impossible to repeat quietly.
They are offline: nothing here touches the network.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "measure_trend_alignment.py"

sys.path.insert(0, str(REPO / "scripts"))
import measure_trend_alignment as m  # noqa: E402


def test_script_exists():
    assert SCRIPT.is_file()


def test_missing_symbol_is_fatal():
    with pytest.raises(m.MeasurementDataError) as exc:
        m.require_complete({}, ["META", "AAPL"])
    assert "META" in str(exc.value) and "AAPL" in str(exc.value)


def test_partial_history_is_fatal():
    with pytest.raises(m.MeasurementDataError) as exc:
        m.require_complete({"META": [None] * (m.MIN_BARS - 1)}, ["META"])
    assert str(m.MIN_BARS) in str(exc.value)


def test_complete_history_is_accepted():
    m.require_complete({"META": [None] * m.MIN_BARS}, ["META"])


def test_table_of_zeros_is_fatal():
    with pytest.raises(m.MeasurementDataError):
        m.require_nonempty_result({k: {"n": 0} for k in m.SHAPES}, 19)


def test_no_symbol_measured_is_fatal():
    with pytest.raises(m.MeasurementDataError):
        m.require_nonempty_result({"CHAND": {"n": 5}}, 0)


def test_bulk_frame_keyed_by_field_not_ticker_is_fatal():
    """The exact yfinance shape that caused the empty measurement."""

    class _Cols:
        nlevels = 2

        def get_level_values(self, i):
            return ["Close", "Open"] if i == 0 else ["AAPL", "AAPL"]

    class _Frame:
        columns = _Cols()

    with pytest.raises(m.MeasurementDataError) as exc:
        m.split_bulk_frame(_Frame(), "META")
    assert "META" in str(exc.value)


def test_empty_position_set_exits_non_zero():
    """No positions means no measurement -- never an empty but successful run."""
    p = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode != 0, p.stdout
    assert "EMPTY" in p.stderr
    assert "shape" not in p.stdout  # no table was printed


def test_unfetchable_symbol_exits_non_zero_and_prints_no_table(tmp_path):
    """A symbol whose bars cannot be obtained ends the run before any table."""
    pos = tmp_path / "p.json"
    pos.write_text(json.dumps(
        {"ZZZZ-NOT-A-REAL-TICKER": {"entry": "2026-09-01", "entry_px": 1.0, "side": "long"}}
    ))
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--positions", str(pos),
         "--cache", str(tmp_path / "c.json"), "--cache-only"],
        capture_output=True, text=True,
    )
    assert p.returncode != 0, p.stdout
    assert "FATAL" in p.stderr
    assert "gb60 med" not in p.stdout


def test_self_test_mode_passes():
    p = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "SELF-TEST OK" in p.stdout
    assert not any(line.startswith("FAIL ") for line in p.stdout.splitlines())


def test_windows_are_desk_quantities_not_picks():
    """No new number: both measurement windows come from the desk's own code."""
    from src.data.levels import MAX_HORIZON_SESSIONS
    from src.data.technical import ATR_PERIOD

    assert m.REF == MAX_HORIZON_SESSIONS
    assert m.FWD == ATR_PERIOD
