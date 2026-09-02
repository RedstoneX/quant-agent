"""The `pm_selection` grader must actually discriminate.

`ops/` is not collected by pytest, so a grading bug there fails silently
until the next sweep — which costs real money and is exactly when the
harness has to work (same reasoning as
tests/test_model_policy_harness_imports.py, which caught that once already).

These tests never call a model. They drive `_pm_selection_grade` with
hand-built `PortfolioDecision` objects and assert that the score separates
the four behaviours the scenario exists to tell apart:

  1. selection on the evidence            -> 1.00
  2. selection on the sub-floor majority  -> low, and it is the trap check
                                             that fails
  3. selection on famous names            -> `familiarity_bias` fails and
                                             REPORTS the share
  4. doing nothing at all                 -> the live desk's own failure,
                                             graded as failure

The fixture facts they assert against (59 reads, 38 actionable, 30 below the
1.5 R/R floor, 5 qualified shorts, every covered mega-cap weak) are the real
numbers from production run-64290730, not chosen by the test.
"""

from __future__ import annotations

import importlib

import pytest

from src.models import PortfolioDecision, ReasoningChain, TargetPosition


scenarios = importlib.import_module("ops.model_policy.scenarios")


def _chain() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="Risk-on, 90% target invested.",
        news_check="Bearish briefing; sizing reduced.",
        earnings_check="No JUST FILED name is being increased.",
        signal_conflicts="Adjudicated per symbol.",
        sizing_logic="Sized to conviction and reward/risk.",
        portfolio_balance="No sector above the cap.",
        cash_target="Closing part of the deployment gap.",
    )


def _decision(*targets: TargetPosition) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_chain(),
        targets=list(targets),
        portfolio_view="Test book.",
    )


def _target(symbol: str, *, direction: str = "long", risk: float = 1.0,
            catalyst: str = "", conviction: str = "medium") -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        direction=direction,
        risk_allocation_pct=risk,
        conviction=conviction,
        thesis=f"{symbol} thesis for the grader.",
        thesis_invalid_if="Closes through the level.",
        catalyst=catalyst,
    )


def _score(checks) -> float:
    total = sum(c.weight for c in checks) or 1.0
    return sum(c.weight for c in checks if c.passed) / total


def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


# --------------------------------------------------------------------------
# The fixture is the real run, and the checks mean what its numbers mean
# --------------------------------------------------------------------------

def test_fixture_is_the_real_run_and_has_the_documented_shape():
    assert scenarios._SELECTION["_provenance"]["run_id"] == "run-64290730"
    assert scenarios._SELECTION_SHAPE == scenarios._SELECTION_SHAPE_EXPECTED


def test_the_evidence_famous_split_is_real_not_planted():
    """The two highest-conviction calls of the day are unglamorous AND weak,
    the best reward/risk is mostly short, and every covered mega-cap is weak.
    If a future fixture loses that contrast the scenario stops measuring
    anything, so it is asserted rather than assumed."""
    rr = scenarios._SELECTION_RR
    by_symbol = scenarios._SELECTION_BY_SYMBOL

    assert by_symbol["SLB"].rating == "strong_buy"
    assert by_symbol["SLB"].conviction == "high"
    assert rr["SLB"] == 1.28 < scenarios._SELECTION_RR_FLOOR
    assert by_symbol["AGX"].rating == "sell"
    assert by_symbol["AGX"].conviction == "high"
    assert rr["AGX"] == 0.84 < scenarios._SELECTION_RR_FLOOR

    assert scenarios._SELECTION_QUALIFIED_SHORTS == {
        "NKE", "GEV", "UNH", "NEE", "FLNC",
    }
    # Every famous name with a technical read this session is below the floor.
    assert scenarios._SELECTION_FAMOUS_WEAK == {"NVDA", "MSFT", "AAPL", "GOOGL"}
    for symbol in scenarios._SELECTION_FAMOUS_WEAK:
        assert rr[symbol] < scenarios._SELECTION_RR_FLOOR


def test_check_weights_sum_to_one():
    checks = scenarios._pm_selection_grade(_decision(_target("XLE")))
    assert sum(c.weight for c in checks) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Discrimination
# --------------------------------------------------------------------------

def test_evidence_led_selection_scores_full_marks():
    """Four picks, all clearing the 1.5 floor, including two of the five
    qualified shorts. Nothing famous, nothing sub-floor."""
    decision = _decision(
        _target("XLE"),                       # buy/high,  rr 1.67
        _target("PFE"),                       # buy/medium, rr 1.50
        _target("NKE", direction="short"),    # sell/medium, rr 2.28
        _target("GEV", direction="short"),    # sell/medium, rr 2.12
    )
    checks = scenarios._pm_selection_grade(decision)
    assert _score(checks) == pytest.approx(1.0)
    assert all(c.passed for c in checks)


def test_all_trap_selection_scores_low_and_fails_the_trap_check():
    """Five sub-floor candidates, no catalyst on any of them, no short. All
    five are real candidates the model was genuinely offered."""
    decision = _decision(
        _target("CVX"),   # buy/medium,  rr 0.39
        _target("JNJ"),   # buy/medium,  rr 0.60
        _target("MU"),    # buy/medium,  rr 0.78
        _target("COP"),   # buy/medium,  rr 0.76
        _target("TSM"),   # buy/medium,  rr 0.83
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["rr_floor_discipline"].passed is False
    assert checks["selection_from_qualified_set"].passed is False
    assert checks["takes_a_qualified_short"].passed is False
    assert checks["familiarity_bias"].passed is True      # weak, but not famous
    assert _score(list(checks.values())) == pytest.approx(0.35)


def test_famous_but_weak_selection_trips_the_familiarity_check():
    """The failure mode the scenario was built to name: three mega-caps whose
    own reward/risk is below the floor, while five qualified shorts and three
    qualified longs go untaken."""
    decision = _decision(
        _target("NVDA"),   # rr 1.03
        _target("AAPL"),   # rr 1.02
        _target("MSFT", risk=0.5),   # held; risk 0.5% implies ~10.8% weight
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    familiarity = checks["familiarity_bias"]
    assert familiarity.passed is False
    assert "3/3" in familiarity.detail and "100%" in familiarity.detail
    # The passed-over qualified names have to be named, or the number is not
    # actionable to whoever reads the result file.
    assert "NKE" in familiarity.detail and "XLE" in familiarity.detail
    assert checks["selection_from_qualified_set"].passed is False
    assert checks["takes_a_qualified_short"].passed is False


def test_familiarity_number_is_reported_even_when_the_check_passes():
    """The owner asked for the diagnostic on every run, not only on a
    failure."""
    checks = _by_name(scenarios._pm_selection_grade(_decision(_target("XLE"))))
    familiarity = checks["familiarity_bias"]
    assert familiarity.passed is True
    assert "famous-but-weak picks 0/1 (0%)" in familiarity.detail


def test_doing_nothing_reproduces_the_live_failure_and_is_graded_as_failure():
    """run-64290730 executed zero trades against 38 actionable signals.
    Matching the live desk must not score."""
    checks = _by_name(scenarios._pm_selection_grade(_decision()))
    assert checks["parsed_and_grounded"].passed is True
    assert checks["opens_a_position"].passed is False
    assert checks["selection_from_qualified_set"].passed is False
    assert checks["takes_a_qualified_short"].passed is False
    # No vacuous credit: an empty book cannot pass the discipline or the
    # familiarity check by having nothing to judge.
    assert checks["rr_floor_discipline"].passed is False
    assert checks["familiarity_bias"].passed is False
    assert _score(list(checks.values())) == pytest.approx(0.10)


def test_inaction_does_not_outscore_a_bad_but_real_book():
    """The vacuous-pass bug this guards against: gating nothing on `picks`
    scored the empty book 0.40 and an all-sub-floor book 0.35, so declining
    to trade beat trading badly. Inaction is the live desk's own failure and
    must sit at the bottom."""
    nothing = _score(scenarios._pm_selection_grade(_decision()))
    traps = _score(scenarios._pm_selection_grade(
        _decision(_target("CVX"), _target("JNJ"), _target("MU")),
    ))
    assert nothing < traps


def test_the_live_desks_own_targets_do_not_score_well():
    """The three long targets the production PM actually emitted, replayed
    against this grader. It must not come out looking like good selection."""
    decision = _decision(
        _target("XLE"),                 # rr 1.67 — qualified
        _target("CHPX", conviction="low"),   # rr 3.03 — qualified
        _target("NVDA", catalyst="Strategic $3B investment into SB Energy."),
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["selection_from_qualified_set"].passed is True   # 2 of 3
    assert checks["rr_floor_discipline"].passed is True            # NVDA cited one
    assert checks["takes_a_qualified_short"].passed is False       # the real gap
    assert checks["familiarity_bias"].passed is False              # NVDA is weak
    assert _score(list(checks.values())) == pytest.approx(0.60)


def test_unparsed_decision_scores_zero():
    checks = scenarios._pm_selection_grade(None)
    assert _score(checks) == pytest.approx(0.0)
    assert len(checks) == 1


# --------------------------------------------------------------------------
# What counts as a SELECTION
# --------------------------------------------------------------------------

def test_a_catalyst_makes_a_sub_floor_pick_legal():
    """SLB is the day's only strong_buy and its reward/risk is 1.28. Backing
    it with a named catalyst is exactly what the prompt permits, so the trap
    check must not punish it."""
    decision = _decision(
        _target("SLB", catalyst="Gap-up breakout on 142% volume expansion."),
        _target("NKE", direction="short"),
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["rr_floor_discipline"].passed is True
    assert checks["takes_a_qualified_short"].passed is True
    # Half the picks clear the floor, which is the stated bar.
    assert checks["selection_from_qualified_set"].passed is True


def test_closing_and_trimming_held_names_is_not_scored_as_selection():
    """Book management is not a choice about which candidate to back. A close
    on a held name plus a trim must leave the selection checks looking at the
    one genuine pick only."""
    close = TargetPosition(
        symbol="DIS", risk_allocation_pct=0.0, thesis="Close the starter.",
    )
    trim = _target("MSFT", risk=0.2)   # ~4.3% implied vs 5.1% held -> a trim
    decision = _decision(close, trim, _target("NKE", direction="short"))
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["opens_a_position"].detail.startswith("1 opening/adding target")
    assert checks["familiarity_bias"].passed is True   # the MSFT trim is not a pick
    assert checks["selection_from_qualified_set"].passed is True


def test_adding_to_a_held_name_is_scored_as_selection():
    """The mirror of the trim: raising MSFT's weight IS choosing it, and MSFT
    is a famous name whose real reward/risk was 0.85."""
    decision = _decision(_target("MSFT", risk=0.5))
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["opens_a_position"].passed is True
    assert checks["familiarity_bias"].passed is False


def test_an_unqualified_short_does_not_satisfy_the_short_check():
    """Ten of the fifteen bearish candidates are below the floor. Taking one
    of those is not the same as taking one of the five that qualified, and
    the detail has to show which was which."""
    decision = _decision(_target("AVGO", direction="short"))   # rr 0.46
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["takes_a_qualified_short"].passed is False
    assert "other shorts=['AVGO']" in checks["takes_a_qualified_short"].detail
