"""The gate. A new trade-governing number with no source fails the build.

`pytest` is the check branch protection requires, so this file — not a
reporting script somebody runs by hand — is what makes the no-arbitrary-numbers
rule mechanical. Read `src/number_sources.py`'s docstring for the scope rules
and for the honest list of what this cannot catch.

The tests below are in two groups. The first is the gate itself, against the
live tree. The second proves each failure mode actually fires, using synthetic
fixtures — because a gate nobody has seen fail is indistinguishable from a
gate that passes everything, which is how the desk's `Adversary:` check became
theatre once already.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.number_sources import (
    MAX_ARBITRARY_ENTRIES,
    SCOPED_CONFIG_CLASSES,
    SCOPED_PATHS,
    audit,
    collect_sites,
    load_ledger,
)

# --------------------------------------------------------------------------
# The gate, against the live tree.
# --------------------------------------------------------------------------


def test_every_trade_governing_number_is_accounted_for() -> None:
    """THE GATE. Every numeric definition site in scope has a ledger entry,
    the entry's value still matches the code, every claimed source is
    non-empty, and no derivation has outlived the base it was derived from.

    If this fails on your branch you have added or moved a number that
    governs a trade. Add it to `config/number_ledger.yaml` with where it came
    from. If nothing backs it, say `arbitrary` — and note that raising
    `MAX_ARBITRARY_ENTRIES` to fit it is an owner decision, not a build fix.
    """
    problems = audit()
    assert not problems, "\n".join(
        ["unsourced or unaccounted trade-governing numbers:", ""]
        + [f"  {p}" for p in problems]
    )


def test_the_case_this_gate_was_built_for_is_in_scope() -> None:
    """`ConstructorConfig.min_risk_pct` — a bare dataclass default that drops
    every trade plan under 0.50% of the account — must be a site the scanner
    sees. It is the shape of number this whole check exists for: no config
    entry, no document, no ratification record, and a comment beside it
    arguing for a different figure. A scanner that misses it is worthless.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.portfolio_constructor.ConstructorConfig.min_risk_pct" in ids


def test_stop_width_scalers_inside_a_tuple_are_sites() -> None:
    """The stop-width scalers live as numbers inside a tuple of pairs, not as
    bare field defaults, and they multiply into every stop distance. A scanner
    that only reads top-level defaults would not see them.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.portfolio_constructor.ConstructorConfig.stop_atr_setup_scale[1][1]" in ids
    assert "src.portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[0][1]" in ids


def test_result_dataclasses_are_not_sites() -> None:
    """The `*Config` rule is what keeps the signal alive. Result and DTO
    dataclasses in the same scoped files carry numeric defaults too, and
    flagging them would bury the numbers that matter — which is precisely the
    failure that made the earlier whole-codebase idea unworkable.
    """
    ids = {site.site_id for site in collect_sites()}
    assert not [i for i in ids if ".GrossCeilingOutcome." in i]
    assert not [i for i in ids if ".PortfolioVolEstimate." in i]


def test_arbitrary_count_is_ratcheted_to_the_seeded_inventory() -> None:
    """The unsourced list may shrink. It may not grow silently.

    `MAX_ARBITRARY_ENTRIES` is pinned to the count seeded from board item 90.
    LOWER it when a number is genuinely sourced. Raising it records an owner
    decision to add an unsourced trade-governing number, and should not pass
    review without one.
    """
    ledger = load_ledger()
    arbitrary = [e for e in ledger.values() if e.get("status") == "arbitrary"]
    assert len(arbitrary) <= MAX_ARBITRARY_ENTRIES
    assert MAX_ARBITRARY_ENTRIES == 87, (
        "the ratchet moved; if a number was sourced, lower it and say which"
    )


def test_scope_has_not_silently_narrowed() -> None:
    """A scoped path or config class that stopped existing would make the gate
    pass by covering less. `collect_sites` raises on a missing path; this
    pins the scoped config classes for the same reason.
    """
    import src.config as config_module

    for name in SCOPED_CONFIG_CLASSES:
        assert hasattr(config_module, name), f"{name} left src/config.py"
    assert "src/risk" in SCOPED_PATHS
    assert "src/portfolio_constructor.py" in SCOPED_PATHS


# --------------------------------------------------------------------------
# Each failure mode, proven to fire.
# --------------------------------------------------------------------------


def _fixture(tmp_path: Path, module_src: str, ledger_src: str) -> tuple[Path, Path]:
    """A miniature repo: one scoped module plus a ledger, so each rule can be
    tripped in isolation without touching the real tree.
    """
    (tmp_path / "src" / "risk").mkdir(parents=True)
    (tmp_path / "src" / "risk" / "rules.py").write_text(textwrap.dedent(module_src))
    (tmp_path / "src" / "config.py").write_text("class RiskConfig:\n    pass\n")
    for entry in SCOPED_PATHS:
        target = tmp_path / entry
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".py":
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(textwrap.dedent(ledger_src))
    return tmp_path, ledger


def _kinds(root: Path, ledger: Path) -> set[str]:
    return {p.kind for p in audit(repo_root=root, ledger_path=ledger)}


def test_a_new_number_with_no_entry_fails() -> None:
    """Rule 1, COVERAGE — the whole point of the gate."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        "numbers: []\n",
    )
    assert "unsourced" in _kinds(root, ledger)


def test_changing_a_number_without_touching_its_entry_fails() -> None:
    """Rule 2, VALUE — a number may not outlive its recorded justification."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 3.0
            status: arbitrary
            note: nothing behind it
        """,
    )
    assert "value-drift" in _kinds(root, ledger)


def test_claiming_a_source_without_writing_one_fails() -> None:
    """Rule 3, GROUNDS — `sourced` is not a word you may just type."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: "   "
        """,
    )
    assert "no-source" in _kinds(root, ledger)


def test_a_derivation_whose_base_moved_fails() -> None:
    """Rule 4, BASE DRIFT — the class the brief asked about by name.

    A number is derived from a base and both are recorded. The base then
    changes, as `min_stop_atr_multiple` did on 2026-09-10 (commit 0088328c,
    3.0 -> 2.5). The derivation no longer describes the live geometry, so the
    derived number is arbitrary again, and the build says so instead of
    letting a stale justification sit there looking respectable.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 1.5
            source: tightest scaler keeping the stop outside the noise band
        """,
    )
    assert "base-drift" in _kinds(root, ledger)


def test_a_derivation_whose_base_still_holds_passes() -> None:
    """The other half of rule 4: it must not cry wolf while the base stands.
    A base-drift check that fires on a valid derivation would be routed around
    inside a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 2.5
            source: tightest scaler keeping the stop outside the noise band
        """,
    )
    assert not _kinds(root, ledger)


def test_a_renamed_or_deleted_number_leaves_a_detectable_orphan() -> None:
    """A stale entry would let coverage look complete while the code moved."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "# the constant was deleted\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
            note: nothing behind it
        """,
    )
    assert "orphan" in _kinds(root, ledger)


def test_zero_and_one_are_not_sites() -> None:
    """Neither is a chosen magnitude — 0 is an empty default and 1 is the
    identity. Including them would add ~40 entries no reviewer could say
    anything about, which is how a check earns a reputation for noise.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "NEUTRAL_ZERO = 0.0\nIDENTITY = 1.0\n",
        "numbers: []\n",
    )
    assert not _kinds(root, ledger)
