"""The stop-multiple sweep must bracket every value the live code can place.

A research sweep that does not span the number it is measuring is not a
measurement. This pins the bracket against the live config rather than against
a copy of it, so moving `min_stop_atr_multiple` without widening the sweep is
a red test, not a silently narrow study.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.config import RiskConfig

SCRIPT = Path(__file__).resolve().parents[1] / "ops/research/min_stop_atr_sweep.py"


def _sweep_module():
    spec = importlib.util.spec_from_file_location("min_stop_atr_sweep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sweep_brackets_the_live_multiple_and_the_absolute_floor():
    mod = _sweep_module()
    fields = RiskConfig.model_fields
    live = fields["min_stop_atr_multiple"].default
    floor = fields["absolute_min_stop_atr_multiple"].default
    lo, hi = min(mod.MULTIPLES), max(mod.MULTIPLES)
    assert lo < floor, f"sweep starts at {lo}, not below the code's absolute floor {floor}"
    assert lo < live < hi, f"sweep {lo}..{hi} does not bracket the live min_stop_atr_multiple {live}"
    assert lo < 1.5 < hi, "sweep must also bracket the earlier 1.5 MAE reading"


def test_sweep_reports_more_than_one_horizon():
    mod = _sweep_module()
    assert len(mod.HORIZONS) > 1, (
        "the repo pins no default holding horizon, so a single-horizon sweep would be a picked number"
    )
