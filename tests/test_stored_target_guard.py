"""The bug-fix backfill entry point, and the guard that makes the next
derivation bug visible instead of silent.

Background. Commit 8f4f77c4 fixed `derive_structural_target`: the noise
floor had been FILTERING the candidate levels, so a wall inside one ATR of
entry left the candidate set and the target was promoted to the next level
out. The code was corrected; the numbers already frozen on
`trades.take_profit` for positions opened before it were not, and nothing
compared a stored target against what the code says today.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from src.risk import target_revision as tr
from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE
from scripts.check_stored_targets import (
    FINDING_AIMS_PAST_WALL,
    FINDING_DRIFT,
    walls_between,
)


# --------------------------------------------------------------------------
# The correction re-asks the ORIGINAL question with working code.
# --------------------------------------------------------------------------

def test_the_backfill_returns_the_nearest_wall_on_the_meta_case():
    """The desk's own reproduced case (PR #790): the 2026-09-21 META add at
    $728.41 with ATR $21.22 put the old noise floor at $749.63, which
    swallowed the computed resistances at $730.41 and $739.84 and promoted
    the stored target to $785.20 — above both rejections.

    Re-derived by the backfill entry point from the same pinned inputs, the
    answer is the nearest wall.
    """
    out = tr.assess_bugfix_backfill(
        symbol="META", direction="long", entry_price=728.41,
        stored_target=785.20, pinned_horizon_sessions=12,
        setup_type="breakout",
        levels=[672.94, 688.26, 707.03, 730.41, 739.84, 784.58],
        atr=21.22, close_price=725.00, levels_coverage="full",
    )
    assert out.new_price == 730.41
    assert out.code == tr.TRIGGER_DERIVATION_CORRECTED
    assert out.basis == tr.STRUCTURAL_LEVEL_BASIS


def test_the_correction_is_its_own_code_and_never_a_market_trigger():
    """A code deploy must not be recorded as a market event. The backfill's
    code is distinct from every trigger the revision path can fire, so a
    reader of `specialist_evidence` separates the two on the code alone."""
    assert tr.TRIGGER_DERIVATION_CORRECTED not in {
        tr.TRIGGER_LEVEL_BROKEN,
        tr.TRIGGER_TARGET_BEYOND_REACH,
        tr.TRIGGER_TARGET_INSIDE_NOISE,
    }


def test_the_backfill_holds_entry_horizon_and_setup_and_takes_no_current_price():
    """It may read today's bars for levels, ATR and coverage. It may not be
    handed a replacement entry, horizon or setup — those are the pinned
    values, and a target derived from the current price is the one thing
    doctrine forbids outright."""
    params = inspect.signature(tr.assess_bugfix_backfill).parameters
    for pinned in ("entry_price", "pinned_horizon_sessions", "setup_type"):
        assert pinned in params
    # `close_price` exists ONLY to answer "has price already passed this"
    # and to drop levels price has closed through — never as an anchor.
    assert "close_price" in params
    assert "sessions_held" not in params, (
        "sessions_held only feeds the re-anchor, which a correction may "
        "never use"
    )


# --------------------------------------------------------------------------
# The one behavioural difference from a revision, pinned both ways.
# --------------------------------------------------------------------------

_REANCHOR_CASE = dict(
    sym="T", direction="long", is_short=False, entry=100.0, target=130.0,
    target_level=None, horizon=10, setup_type="range",
    levels=[105.0, 140.0], vol=4.0, close=125.0, levels_coverage="full",
    trigger="X", sessions_held=3,
    min_target_atr_multiple=1.0, breakout_projection_atr_multiple=1.0,
    max_reach_atr_multiple=1.5, max_horizon_sessions=60,
    break_margin_atr_multiple=BREAK_CONFIRMATION_ATR_MULTIPLE,
)


def test_a_revision_may_reanchor_on_the_remaining_horizon():
    """Item 114, unchanged: a revision whose entry-anchored reach has been
    outrun re-anchors on the latest close over the REMAINING horizon."""
    out = tr._rederive_on_todays_bars(allow_reanchor=True, **_REANCHOR_CASE)
    assert out.basis == tr.REANCHORED_BASIS
    assert out.new_price == 140.0


def test_a_correction_may_not_reanchor_and_refuses_instead():
    """Same inputs, correction semantics. The re-anchor is defined to only
    ever move a target FURTHER from entry, and the whole content of a
    correction is that the stored number already sits too far out — so a
    correction that could re-anchor would silently undo itself. The honest
    answer is the named refusal, with the stored target left standing."""
    out = tr._rederive_on_todays_bars(allow_reanchor=False, **_REANCHOR_CASE)
    assert out.new_price is None
    assert out.code == tr.REVISION_BEHIND_PRICE
    assert "no re-anchor was attempted" in out.detail


def test_there_is_exactly_one_rederivation_body():
    """Both entry points route through `_rederive_on_todays_bars`. A second
    implementation of the same number is how a desk ends up with a target
    that disagrees with itself depending on which caller asked."""
    # The helper was lifted into its own module (2026-10-08 split); every
    # target_revision* module is read, so a call cannot escape by moving file.
    files = sorted(pathlib.Path(tr.__file__).parent.glob("target_revision*.py"))
    home = pathlib.Path(tr.__file__).with_name("target_revision_rederive.py")
    assert home in files
    for path in files:
        body = path.read_text()
        if path != home:
            assert "derive_structural_target(" not in body, (
                f"a derivation call escaped the shared helper into {path.name}"
            )
    body = home.read_text()
    assert body.count("derive_structural_target(") >= 1
    # Every call to the levels derivation lives inside the shared helper.
    helper_start = body.index("def _rederive_on_todays_bars")
    before = body[:helper_start]
    assert "derive_structural_target(" not in before, (
        "a derivation call escaped the shared helper"
    )


# --------------------------------------------------------------------------
# The guard's predicate: pure, and testable without bars.
# --------------------------------------------------------------------------

def test_a_wall_between_entry_and_target_is_the_finding():
    assert walls_between(
        stored_target=785.20, reference_price=710.62,
        surviving_levels=[688.26, 730.41, 739.84, 900.0], is_short=False,
    ) == [730.41, 739.84]


def test_a_target_sitting_on_its_own_wall_is_not_a_finding():
    """The correct outcome is not an error. Strict inequalities both ends."""
    assert walls_between(
        stored_target=730.41, reference_price=710.62,
        surviving_levels=[730.41], is_short=False,
    ) == []


def test_levels_behind_the_entry_are_not_walls():
    assert walls_between(
        stored_target=120.0, reference_price=100.0,
        surviving_levels=[80.0, 95.0, 130.0], is_short=False,
    ) == []


def test_the_short_side_is_the_mirror_and_nearest_comes_first():
    assert walls_between(
        stored_target=90.79, reference_price=93.78,
        surviving_levels=[92.82, 91.71, 88.0, 95.54], is_short=True,
    ) == [92.82, 91.71]


@pytest.mark.parametrize("bad", [None, 0.0, -5.0, "x"])
def test_an_unreadable_input_finds_nothing_rather_than_guessing(bad):
    assert walls_between(
        stored_target=bad, reference_price=100.0,
        surviving_levels=[110.0], is_short=False,
    ) == []
    assert walls_between(
        stored_target=120.0, reference_price=bad,
        surviving_levels=[110.0], is_short=False,
    ) == []


def test_drift_and_the_wall_finding_are_different_severities():
    """A stored target that merely differs from today's derivation is
    ordinary: the derivation reads today's bars and the desk deliberately
    does not re-derive on a price move. Only a standing wall in front of
    the target is an error, or the check fires every session and nobody
    reads it."""
    assert FINDING_AIMS_PAST_WALL != FINDING_DRIFT
    source = pathlib.Path(
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts" / "check_stored_targets.py",
    ).read_text()
    assert "return 1 if bad else 0" in source
    assert 'r.get("finding") == FINDING_AIMS_PAST_WALL' in source


# --------------------------------------------------------------------------
# The guard is SCHEDULED, not a merge gate, and it says the true state.
# --------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).resolve().parent.parent


def test_the_guard_is_wired_to_a_timer_and_a_wrapper():
    """Wired to nothing is the same as not existing. A unit, a timer and a
    wrapper that sources `.env` — the shape every other read-only check on
    this box uses, so `scripts/merge_and_deploy.sh` installs it with no
    change (copy + daemon-reload + enable)."""
    service = _REPO / "scripts/systemd/quant-agent-stored-target-check.service"
    timer = _REPO / "scripts/systemd/quant-agent-stored-target-check.timer"
    wrapper = _REPO / "scripts/run_stored_target_check.sh"
    for path in (service, timer, wrapper):
        assert path.is_file(), path
    # The wrapper is the entry point, not the venv Python — without `.env`
    # the notifier disables itself and the market-data credentials are
    # missing, so a finding would neither be measured nor delivered.
    svc = service.read_text()
    assert "run_stored_target_check.sh" in svc
    assert 'source "${PROJECT_ROOT}/.env"' in wrapper.read_text()
    # Exit 1 is a finding about the book, not a unit failure.
    assert "SuccessExitStatus=0 1" in svc
    # The timer must actually be installable, or the deploy enables nothing.
    assert "[Install]" in timer.read_text()
    assert "WantedBy=timers.target" in timer.read_text()


def test_the_guard_is_not_a_blocking_ci_check():
    """It is legitimately red today on findings the desk cannot correct. A
    permanently-red required check blocks every unrelated merge and is
    switched off within a day, and a switched-off check reports nothing."""
    workflows = list((_REPO / ".github" / "workflows").glob("*.yml")) + list(
        (_REPO / ".github" / "workflows").glob("*.yaml"),
    )
    assert workflows, "no workflows found — the assertion below is vacuous"
    for wf in workflows:
        assert "check_stored_targets" not in wf.read_text(), wf


def test_the_report_names_what_it_cannot_correct_rather_than_going_quiet():
    """The owner's standing rule: a finding the desk cannot correct is
    reported as exactly that — the true state, named, not an error and not
    silence. And no number is substituted for the refusal."""
    from scripts.check_stored_targets import format_message

    msg = format_message([
        {"symbol": "AAPL", "finding": FINDING_AIMS_PAST_WALL,
         "stored_target": 359.93, "derived_target": 344.81},
        {"symbol": "META", "finding": FINDING_AIMS_PAST_WALL,
         "stored_target": 785.20, "derived_target": None},
    ])
    # The correctable one carries both numbers so the reader can check it.
    assert "$359.93" in msg and "$344.81" in msg
    # The uncorrectable one is named, said to be wrong, and said to have no
    # replacement — and its stored number is never swapped for a guess.
    assert "META" in msg and "$785.20" in msg
    assert "CANNOT recompute" in msg
    assert "not inventing a number" in msg


def test_a_clean_book_says_nothing():
    """A check that speaks every session is a check nobody reads, and a
    daily all-clear is how a real finding gets scrolled past."""
    from scripts.check_stored_targets import FINDING_AGREES, format_message

    assert format_message([]) == ""
    assert format_message([
        {"symbol": "ETN", "finding": FINDING_DRIFT, "stored_target": 487.69},
        {"symbol": "UPS", "finding": FINDING_AGREES, "stored_target": 92.82},
    ]) == ""


def test_the_wall_test_has_exactly_one_definition():
    """The scheduled report and the live revision path must never disagree
    about whether a wall is in the way, so there is one function and the
    script imports it."""
    assert walls_between is tr.walls_between
