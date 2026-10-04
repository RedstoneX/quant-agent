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


# --------------------------------------------------------------------------- #
# The condition roster is pinned. Shortening the gate's list — dropping a
# condition, or quietly moving one out of ACTIVATION scope so the live switch
# stops asking about it — must fail a test. That is the whole point of item 150:
# the checklist stops being prose that can be edited away without anyone
# noticing.
# --------------------------------------------------------------------------- #
EXPECTED_MECHANICAL_IDS = (
    "paper_only_guard_active",
    "settings_declare_paper",
)

EXPECTED_MANUAL_IDS = (
    "explicit_owner_approval",
    "strategy_earned_live_capital",
    "broker_capabilities_reviewed",
    "threat_model_and_credential_design",
    "governor_and_sentinel_specs",
    "breaker_thresholds_and_escalation_matrix",
    "broker_local_reconciliation",
    "failure_mode_tests_passed",
    "deployment_change_control_incident_recovery",
    "capital_promotion_criteria",
    "live_readiness_review_signed",
    "margin_2x_rederived_for_live",
)


def test_condition_roster_is_pinned():
    assert tuple(c.condition_id for c in lcp.MECHANICAL_CONDITIONS) == \
        EXPECTED_MECHANICAL_IDS
    assert tuple(c.condition_id for c in lcp.MANUAL_CONDITIONS) == \
        EXPECTED_MANUAL_IDS


def test_activation_scope_roster_is_pinned(tmp_path):
    """Every manual condition is evaluated at the live switch, and none may be
    dropped from that scope without this failing."""
    att = _write(tmp_path / "att.yaml", yaml.safe_dump({"attestations": {}}))
    gate = lcp.evaluate(
        settings_path=_paper_settings(tmp_path),
        attestations_path=att,
        scope=lcp.ACTIVATION,
    )
    assert tuple(r.condition_id for r in gate.results) == EXPECTED_MANUAL_IDS


def test_audit_scope_covers_every_condition(tmp_path):
    att = _write(tmp_path / "att.yaml", yaml.safe_dump({"attestations": {}}))
    gate = lcp.evaluate(
        settings_path=_paper_settings(tmp_path),
        attestations_path=att,
        scope=lcp.AUDIT,
    )
    assert tuple(r.condition_id for r in gate.results) == \
        EXPECTED_MECHANICAL_IDS + EXPECTED_MANUAL_IDS


def test_unknown_scope_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        lcp.evaluate(
            settings_path=_paper_settings(tmp_path),
            attestations_path=tmp_path / "missing.yaml",
            scope="whatever",
        )


# --------------------------------------------------------------------------- #
# assert_live_capital_authorized — the callable the paper-only guard uses.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cond", lcp.MANUAL_CONDITIONS, ids=lambda c: c.condition_id)
def test_activation_refuses_when_any_single_condition_unmet(tmp_path, cond):
    data = _all_attested()
    data["attestations"][cond.condition_id]["attested"] = False
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(data))
    with pytest.raises(lcp.LiveCapitalBlocked) as exc:
        lcp.assert_live_capital_authorized(
            settings_path=_paper_settings(tmp_path), attestations_path=att
        )
    assert cond.condition_id in str(exc.value)


def test_activation_passes_when_all_attested(tmp_path):
    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))
    gate = lcp.assert_live_capital_authorized(
        settings_path=_paper_settings(tmp_path), attestations_path=att
    )
    assert gate.passed


def test_activation_fails_closed_on_missing_attestation_file(tmp_path):
    with pytest.raises(lcp.LiveCapitalBlocked):
        lcp.assert_live_capital_authorized(
            settings_path=_paper_settings(tmp_path),
            attestations_path=tmp_path / "nope.yaml",
        )


# --------------------------------------------------------------------------- #
# The gate sits at the lock point: src/config.py AlpacaConfig.
# --------------------------------------------------------------------------- #
def test_live_trading_is_not_authorized_in_the_shipped_code():
    from src import config as cfg

    assert cfg.LIVE_TRADING_AUTHORIZED is False


def test_config_refuses_live_when_not_code_authorized():
    from src.config import AlpacaConfig

    with pytest.raises(Exception) as exc:
        AlpacaConfig(base_url="https://api.alpaca.markets", paper=False)
    assert "not authorized" in str(exc.value)


def test_config_refuses_live_when_gate_blocks(monkeypatch):
    """Code-authorized but a condition unmet: refused, and the refusal names it."""
    from src import config as cfg

    monkeypatch.setattr(cfg, "LIVE_TRADING_AUTHORIZED", True)
    with pytest.raises(Exception) as exc:
        cfg.AlpacaConfig(base_url="https://api.alpaca.markets", paper=False)
    message = str(exc.value)
    assert "pre-flight gate" in message
    assert "explicit_owner_approval" in message


def test_config_allows_live_only_when_code_authorized_and_gate_passes(
    monkeypatch, tmp_path
):
    """The one path to a live account: code-authorized AND every condition met."""
    from src import config as cfg

    att = _write(tmp_path / "att.yaml", yaml.safe_dump(_all_attested()))
    settings = _paper_settings(tmp_path)

    real = lcp.assert_live_capital_authorized

    def _passing(**_kwargs):
        return real(settings_path=settings, attestations_path=att)

    monkeypatch.setattr(cfg, "LIVE_TRADING_AUTHORIZED", True)
    monkeypatch.setattr(lcp, "assert_live_capital_authorized", _passing)

    conf = cfg.AlpacaConfig(base_url="https://api.alpaca.markets", paper=False)
    assert conf.paper is False
