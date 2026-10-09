"""The four 1.0 ATR multiples must stay four SEPARATE, honestly-marked numbers.

Board item 70 was opened because one `1.0` literal did two different jobs in the
exit path. The split is built: the adverse-move noise band, the break-confirmation
margin, the last-resort fallback protection margin and the absolute minimum stop
multiple are four distinct names. Item 213 carries the leftover — none of them is
sourced yet.

This test pins BEHAVIOUR IS UNCHANGED (all four still read 1.0) and pins the
SEPARATION (they are four names, not one shared constant), so a future sourcing
pass cannot silently re-collapse them or retune one by moving another.

`FALLBACK_PROTECTION_ATR_MULTIPLE` is the newest and least-guarded of the four
(split out of the noise band on 2026-10-04) and its ledger row was covered by
nothing here, so the one layer at which a re-collapse has actually happened
before — ledger prose and `derived_from` — was unguarded for it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.config import RiskConfig
from src.portfolio_constructor import ConstructorConfig
from src.risk.exit_guard import (
    BREAK_CONFIRMATION_ATR_MULTIPLE,
    FALLBACK_PROTECTION_ATR_MULTIPLE,
    NOISE_BAND_ATR_MULTIPLE,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER = _REPO_ROOT / "config" / "number_ledger.yaml"

_EXIT_GUARD_IDS = (
    "src.risk.exit_guard.NOISE_BAND_ATR_MULTIPLE",
    "src.risk.exit_guard.BREAK_CONFIRMATION_ATR_MULTIPLE",
    "src.risk.exit_guard.FALLBACK_PROTECTION_ATR_MULTIPLE",
)
_MIN_STOP_ID = "src.config.RiskConfig.absolute_min_stop_atr_multiple"
_MIN_STOP_MIRROR_ID = "src.portfolio_constructor.config.ConstructorConfig.absolute_min_stop_atr_multiple"


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
    """No behaviour change: every one of the four still reads exactly 1.0."""
    assert NOISE_BAND_ATR_MULTIPLE == 1.0
    assert BREAK_CONFIRMATION_ATR_MULTIPLE == 1.0
    assert FALLBACK_PROTECTION_ATR_MULTIPLE == 1.0
    assert RiskConfig.model_fields["absolute_min_stop_atr_multiple"].default == 1.0
    assert ConstructorConfig().absolute_min_stop_atr_multiple == 1.0


def test_three_distinct_names_not_one_shared_constant() -> None:
    """The jobs must stay separately named so one cannot be retuned via another."""
    import src.risk.exit_guard as exit_guard

    for name in (
        "NOISE_BAND_ATR_MULTIPLE",
        "BREAK_CONFIRMATION_ATR_MULTIPLE",
        "FALLBACK_PROTECTION_ATR_MULTIPLE",
    ):
        assert name in exit_guard.__all__, f"{name} is no longer exported"

    # The minimum stop multiple is a THIRD, config-borne number: it must not be
    # imported from, or aliased to, either exit-path band.
    from src.feature_flags import config_modules

    source = "\n".join(p.read_text() for p in config_modules(_REPO_ROOT))
    assert "absolute_min_stop_atr_multiple" in source
    assert "NOISE_BAND_ATR_MULTIPLE" not in source
    assert "BREAK_CONFIRMATION_ATR_MULTIPLE" not in source
    assert "FALLBACK_PROTECTION_ATR_MULTIPLE" not in source


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


def test_no_ledger_row_says_the_break_margin_is_the_noise_band() -> None:
    """Ledger PROSE must not re-collapse what the 2026-09-26 split separated.

    The `TREND_CONFIRMING_CLOSES` note survived the split still asserting "The
    break MARGIN is always NOISE_BAND_ATR_MULTIPLE", which contradicts
    `check_structural_protection` in src/risk/exit_guard.py: the margin there is
    `BREAK_CONFIRMATION_ATR_MULTIPLE`. A false provenance claim in the ledger is
    the same defect as the shared literal was, one layer up.
    """
    entries = _ledger_entries()
    offenders: list[str] = []
    for ident, entry in entries.items():
        if not isinstance(ident, str) or not ident.startswith("src.risk.exit_guard."):
            continue
        if ident.endswith("NOISE_BAND_ATR_MULTIPLE"):
            continue
        prose = " ".join(str(entry.get(field, "")) for field in ("note", "source")).lower()
        prose = " ".join(prose.split())
        for claim in (
            "break margin is always noise_band_atr_multiple",
            "break margin is noise_band_atr_multiple",
            "margin is always the noise band",
        ):
            if claim in prose:
                offenders.append(f"{ident}: {claim!r}")
    assert not offenders, "ledger prose re-collapses the break margin into the noise band: " + "; ".join(offenders)


def test_noise_band_widening_is_uncapped_and_its_null_is_recorded() -> None:
    """The sqrt widening is uncapped and measured-unsupported (2026-10-04).

    `ops/research/noise_band_holding_scaling.py` found no adverse-excursion
    size at which a trend is finished, at any holding length, so no cap is
    derivable and none was invented. This pins the uncapped state and the
    recorded null together: a future cap must land with the evidence that
    replaces this section, not quietly.
    """
    from math import sqrt

    from src.risk.exit_guard import noise_band_atr

    assert noise_band_atr(60) == sqrt(60)
    assert noise_band_atr(250) == sqrt(250)

    findings = (Path(__file__).resolve().parents[1] / "docs/RESEARCH_FINDINGS.md").read_text()
    assert "Does the band's sqrt(sessions_held) widening match the tape?" in findings
    assert "No value in `exit_guard.py` is changed by this work." in findings


def test_no_exit_guard_multiple_is_derived_from_another() -> None:
    """A `derived_from` between the exit-path bands IS the collapse, one layer up.

    Separation survives a shared literal being deleted only if the ledger also
    refuses to re-tie the jobs together. If any of these rows ever declared
    itself derived from another of them, deriving one would silently move the
    other — exactly the defect board item 70 exists to end — while every value
    assertion above still passed.
    """
    entries = _ledger_entries()
    offenders: list[str] = []
    for ident in _EXIT_GUARD_IDS:
        parent = entries[ident].get("derived_from")
        if isinstance(parent, str) and parent in _EXIT_GUARD_IDS:
            offenders.append(f"{ident} declares derived_from {parent}")
    assert not offenders, "exit-path ATR multiples re-tied to each other in the ledger: " + "; ".join(offenders)
