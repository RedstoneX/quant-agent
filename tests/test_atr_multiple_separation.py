"""The three 1.0 ATR multiples must stay three SEPARATE, honestly-marked numbers.

Board item 70 was opened because one `1.0` literal did two different jobs in the
exit path. The split is built: the adverse-move noise band, the break-confirmation
margin and the absolute minimum stop multiple are three distinct names. Item 213
carries the leftover — none of them is sourced yet.

This test pins BEHAVIOUR IS UNCHANGED (all three still read 1.0) and pins the
SEPARATION (they are three names, not one shared constant), so a future sourcing
pass cannot silently re-collapse them or retune one by moving another.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.config import RiskConfig
from src.portfolio_constructor import ConstructorConfig
from src.risk.exit_guard import (
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    NOISE_BAND_ATR_MULTIPLE,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER = _REPO_ROOT / "config" / "number_ledger.yaml"

_EXIT_GUARD_IDS = (
    "src.risk.exit_guard.NOISE_BAND_ATR_MULTIPLE",
    "src.risk.exit_guard.BREAK_CONFIRMATION_ATR_MULTIPLE",
)
_MIN_STOP_ID = "src.config.RiskConfig.absolute_min_stop_atr_multiple"
_MIN_STOP_MIRROR_ID = (
    "src.portfolio_constructor.ConstructorConfig.absolute_min_stop_atr_multiple"
)


def _ledger_entries() -> dict[str, dict]:
    with open(LEDGER) as fh:
        doc = yaml.safe_load(fh)
    found: dict[str, dict] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ident = node.get("id")
            if isinstance(ident, str):
                found[ident] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return found


def test_values_unchanged() -> None:
    """No behaviour change: every one of the three still reads exactly 1.0."""
    assert NOISE_BAND_ATR_MULTIPLE == 1.0
    assert BREAK_CONFIRMATION_ATR_MULTIPLE == 1.0
    assert RiskConfig.model_fields["absolute_min_stop_atr_multiple"].default == 1.0
    assert ConstructorConfig().absolute_min_stop_atr_multiple == 1.0


def test_three_distinct_names_not_one_shared_constant() -> None:
    """The jobs must stay separately named so one cannot be retuned via another."""
    import src.risk.exit_guard as exit_guard

    for name in ("NOISE_BAND_ATR_MULTIPLE", "BREAK_CONFIRMATION_ATR_MULTIPLE"):
        assert name in exit_guard.__all__, f"{name} is no longer exported"

    # The minimum stop multiple is a THIRD, config-borne number: it must not be
    # imported from, or aliased to, either exit-path band.
    source = (_REPO_ROOT / "src" / "config.py").read_text()
    assert "absolute_min_stop_atr_multiple" in source
    assert "NOISE_BAND_ATR_MULTIPLE" not in source
    assert "BREAK_CONFIRMATION_ATR_MULTIPLE" not in source


def test_ledger_marks_each_job_separately_and_honestly() -> None:
    """Each job carries its OWN entry, marked `arbitrary` while it is unsourced."""
    entries = _ledger_entries()

    for ident in (*_EXIT_GUARD_IDS, _MIN_STOP_ID):
        assert ident in entries, f"{ident} has no ledger entry"
        entry = entries[ident]
        assert entry["status"] == "arbitrary", (
            f"{ident} is marked {entry['status']!r}; it is still unsourced "
            "(item 213) and overstating it is the same failure as understating it"
        )
        assert float(entry["value"]) == 1.0

    # The constructor copy is a checked MIRROR of the config value, deliberately
    # not counted as a fourth arbitrary number; it must keep pointing at its base.
    mirror = entries[_MIN_STOP_MIRROR_ID]
    assert mirror["status"] == "derived"
    assert mirror["derived_from"] == _MIN_STOP_ID
    assert float(mirror["base_value"]) == 1.0
