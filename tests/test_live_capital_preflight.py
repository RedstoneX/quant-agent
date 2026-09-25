"""Tests for the live-capital pre-flight gate (board item 150)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src import live_capital_preflight as lcp

PAPER_SETTINGS = textwrap.dedent(
    """
    alpaca:
      base_url: "https://paper-api.alpaca.markets"
      paper: true
    """
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _all_attested() -> dict:
    return {
        "attestations": {
            c.condition_id: {
                "attested": True,
                "attested_by": "Rex",
                "attested_on": "2026-09-25",
                "note": "verified",
            }
            for c in lcp.MANUAL_CONDITIONS
        }
    }


def _paper_settings(tmp_path: Path) -> Path:
    return _write(tmp_path / "settings.yaml", PAPER_SETTINGS)


# --------------------------------------------------------------------------- #
# Passing: every condition holds.
# --------------------------------------------------------------------------- #
def test_gate_passes_when_all_conditions_hold(tmp_path):
    settings = _paper_settings(tmp_path)
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))

    gate = lcp.evaluate(settings_path=settings, attestations_path=att)

    assert gate.passed is True
    assert gate.blocking == []
    assert all(r.status == lcp.PASS for r in gate.results)
    # main() returns 0 (allow) only in this fully-satisfied state.
    assert lcp.main(["--settings", str(settings), "--attestations", str(att)]) == 0


# --------------------------------------------------------------------------- #
# Blocking: each individual condition failing blocks go-live.
# --------------------------------------------------------------------------- #
def test_default_shipped_attestations_block(tmp_path):
    """The real shipped attestation file must BLOCK (all attested: false)."""
    settings = _paper_settings(tmp_path)
    gate = lcp.evaluate(
        settings_path=settings,
        attestations_path=lcp.DEFAULT_ATTESTATIONS_PATH,
    )
    assert gate.passed is False
    # every manual condition should be BLOCK in the shipped state
    manual = [r for r in gate.results if r.kind == "manual"]
    assert manual and all(r.status == lcp.BLOCK for r in manual)


def test_missing_attestation_file_blocks(tmp_path):
    settings = _paper_settings(tmp_path)
    gate = lcp.evaluate(
        settings_path=settings,
        attestations_path=tmp_path / "does_not_exist.yaml",
    )
    assert gate.passed is False
    assert all(
        r.status == lcp.BLOCK for r in gate.results if r.kind == "manual"
    )


@pytest.mark.parametrize("cond", [c.condition_id for c in lcp.MANUAL_CONDITIONS])
def test_each_manual_condition_blocks_when_unmet(tmp_path, cond):
    settings = _paper_settings(tmp_path)
    data = _all_attested()
    # knock out exactly one condition
    data["attestations"][cond]["attested"] = False
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(data))

    gate = lcp.evaluate(settings_path=settings, attestations_path=att)

    assert gate.passed is False
    blocked_ids = {r.condition_id for r in gate.blocking}
    assert cond in blocked_ids


def test_incomplete_attestation_blocks(tmp_path):
    """attested: true but no attester/date must still BLOCK."""
    settings = _paper_settings(tmp_path)
    data = _all_attested()
    data["attestations"]["explicit_owner_approval"] = {"attested": True}
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(data))

    gate = lcp.evaluate(settings_path=settings, attestations_path=att)

    assert gate.passed is False
    owner = next(
        r for r in gate.results if r.condition_id == "explicit_owner_approval"
    )
    assert owner.status == lcp.BLOCK


def test_settings_not_paper_fails(tmp_path):
    settings = _write(
        tmp_path / "settings.yaml",
        textwrap.dedent(
            """
            alpaca:
              base_url: "https://api.alpaca.markets"
              paper: false
            """
        ),
    )
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))

    gate = lcp.evaluate(settings_path=settings, attestations_path=att)

    assert gate.passed is False
    mech = next(
        r for r in gate.results if r.condition_id == "settings_declare_paper"
    )
    assert mech.status == lcp.FAIL


def test_missing_settings_file_fails(tmp_path):
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))
    gate = lcp.evaluate(
        settings_path=tmp_path / "nope.yaml", attestations_path=att
    )
    assert gate.passed is False
    mech = next(
        r for r in gate.results if r.condition_id == "settings_declare_paper"
    )
    assert mech.status == lcp.FAIL


def test_paper_only_guard_condition_passes(tmp_path):
    """The mechanical guard check passes because the code guard is active."""
    status, _ = lcp._check_paper_only_guard()
    assert status == lcp.PASS


def test_main_blocks_with_nonzero_exit(tmp_path):
    settings = _paper_settings(tmp_path)
    # shipped-style file: all false
    att = _write(
        tmp_path / "att.yaml",
        yaml.safe_dump(
            {
                "attestations": {
                    c.condition_id: {"attested": False}
                    for c in lcp.MANUAL_CONDITIONS
                }
            }
        ),
    )
    rc = lcp.main(["--settings", str(settings), "--attestations", str(att)])
    assert rc == 1


def test_report_names_every_condition(tmp_path):
    settings = _paper_settings(tmp_path)
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))
    gate = lcp.evaluate(settings_path=settings, attestations_path=att)
    report = lcp.format_report(gate)
    for c in lcp.MECHANICAL_CONDITIONS:
        assert c.condition_id in report
    for c in lcp.MANUAL_CONDITIONS:
        assert c.condition_id in report


def test_shipped_attestation_ids_match_code():
    """The shipped attestation file must list exactly the code's manual ids."""
    raw = yaml.safe_load(lcp.DEFAULT_ATTESTATIONS_PATH.read_text())
    file_ids = set(raw["attestations"].keys())
    code_ids = {c.condition_id for c in lcp.MANUAL_CONDITIONS}
    assert file_ids == code_ids
