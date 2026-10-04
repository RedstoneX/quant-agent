"""Witness: the noise band's geometry is a standalone piece. It is imported
and exercised here WITHOUT `src.risk.exit_guard` (or any owner object)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.risk.noise_band_anchor import (
    anchored_adverse_move,
    band_width_atr,
    noise_band_anchor,
)


def test_module_loads_without_exit_guard():
    code = (
        "import sys; import src.risk.noise_band_anchor; "
        "sys.exit(1 if 'src.risk.exit_guard' in sys.modules else 0)"
    )
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": root}
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0


def test_band_width_scales_with_sqrt_sessions_and_floors_at_one():
    assert band_width_atr(4, multiple=2.0) == 4.0
    for bad in (None, 0, -3, float("nan"), "x"):
        assert band_width_atr(bad, multiple=2.0) == 2.0


def test_anchor_long_short_and_fallback():
    assert noise_band_anchor(100.0, 110.0, is_short=False) == 110.0
    assert noise_band_anchor(100.0, 90.0, is_short=False) == 100.0
    assert noise_band_anchor(100.0, 90.0, is_short=True) == 90.0
    assert noise_band_anchor(100.0, 110.0, is_short=True) == 100.0
    for bad in (None, 0, -1.0, float("inf")):
        assert noise_band_anchor(100.0, bad, is_short=False) == 100.0


def test_anchored_adverse_move_mirrors():
    assert anchored_adverse_move(100.0, 107.0, 110.0, is_short=False) == (110.0, 3.0)
    assert anchored_adverse_move(100.0, 93.0, 90.0, is_short=True) == (90.0, 3.0)
    assert anchored_adverse_move(100.0, 95.0, None, is_short=False) == (100.0, 5.0)
