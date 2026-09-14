"""The `pm_selection` grader must actually discriminate.

`ops/` is not collected by pytest, so a grading bug there fails silently
until the next sweep — which costs real money and is exactly when the
harness has to work (same reasoning as
tests/test_model_policy_harness_imports.py, which caught that once already).

These tests never call a model. They drive `_pm_selection_grade` with
hand-built `PortfolioDecision` objects and assert that the score separates
the behaviours the scenario exists to tell apart:

  1. selection from the admitted set, including a short  -> 1.00
  2. selection of names the desk deterministically
     refuses                                             -> the selection
                                                            check fails
  3. selection on famous names                           -> REPORTED as a
                                                            number, never
                                                            scored
  4. doing nothing at all                                -> the live desk's
                                                            own failure,
                                                            graded as failure

**REBUILT 2026-09-14 (docs/WORK.md PM-gate item 8).** Every assertion here
used to be written against `analyst reward/risk >= 1.5`. The owner retired
that floor on 2026-09-11 (item 1(d)) and this grader kept scoring against it,
so a model comparison run on it would have paid to discover which model best
obeys a deleted rule. The admitted set now comes from
`deterministic_selection.evaluate` — the desk's own current admission rules —
and the fixture facts asserted below (59 reads, 38 actionable, 12 of them
breakouts, 25 admitted, 2 admitted shorts) are the real numbers from
production run-64290730 put through those rules, not chosen by the test.
"""

from __future__ import annotations

import importlib

import pytest

from src.models import PortfolioDecision, ReasoningChain, TargetPosition


scenarios = importlib.import_module("ops.model_policy.scenarios")


#: The four checks that carry weight, and the shares they carry. Deliberately
#: NOT rescaled to sum to 1.0 when two checks were removed — see the scenario
#: header. `benchmark_models._run_scenario` divides by the real total.
_SCORING_WEIGHTS = {
    "parsed_and_grounded": 0.10,
    "opens_a_position": 0.10,
    "selection_from_eligible_set": 0.25,
    "takes_an_eligible_short": 0.25,
}
_TOTAL_WEIGHT = 0.70


def _chain() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="Risk-on, 90% target invested.",
        news_check="Bearish briefing; sizing reduced.",
        earnings_check="No JUST FILED name is being increased.",
        signal_conflicts="Adjudicated per symbol.",
        sizing_logic="Sized to conviction and evidence.",
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


def test_the_admitted_set_is_the_desks_own_rules_not_a_ratio():
    """The whole point of the rebuild: `qualified` is now a rule outcome.

    If this ever stops matching `deterministic_selection.evaluate`, the
    grader has grown a second opinion about what the desk admits — which is
    the failure mode that let a retired reward:risk floor survive here for
    three days after the desk deleted it.
    """
    deterministic = importlib.import_module(
        "ops.model_policy.deterministic_selection")
    rows = deterministic.evaluate(
        scenarios._SELECTION,
        scenarios._SELECTION_ANALYSES,
        scenarios._SELECTION_POSITIONS,
        scenarios._SELECTION_NEWS,
    )
    assert scenarios._SELECTION_ELIGIBLE == {
        r["symbol"] for r in rows if r["eligible"]
    }
    assert len(scenarios._SELECTION_ELIGIBLE) == 25


def test_no_check_grades_against_the_retired_reward_risk_floor():
    """The defect this file was rebuilt to remove, pinned so it cannot return.

    A check keyed on a universal 1.5 cannot be reintroduced without failing
    here, and the scenario module must carry no floor constant of its own.
    """
    checks = scenarios._pm_selection_grade(_decision(_target("XLE")))
    for check in checks:
        assert "rr_floor" not in check.name
        assert "qualified" not in check.name
    assert not hasattr(scenarios, "_SELECTION_RR_FLOOR")
    assert not hasattr(scenarios, "_SELECTION_QUALIFIED")


def test_a_thin_but_admitted_pick_costs_nothing():
    """SLB is the day's only `strong_buy`, its analyst ratio is 1.28, and it
    is a BREAKOUT — a setup the prompt forbids judging on reward:risk at all.
    Under the retired floor this pick was a discipline failure unless it
    cited a catalyst. It is now simply a name the desk admits."""
    assert "SLB" in scenarios._SELECTION_BREAKOUT
    assert scenarios._SELECTION_RR["SLB"] == 1.28
    checks = _by_name(scenarios._pm_selection_grade(
        _decision(_target("SLB"), _target("NKE", direction="short")),
    ))
    assert checks["selection_from_eligible_set"].passed is True
    assert checks["takes_an_eligible_short"].passed is True
    assert "n/a[breakout]" in checks["opens_a_position"].detail


def test_the_admitted_shorts_are_the_two_the_desk_would_actually_place():
    """Fifteen bearish candidates, two admitted. Three of the five the old
    grader rewarded (GEV / UNH / NEE) are refused by the §9.4 net-evidence
    rule, so crediting them credited trades production would never place."""
    assert scenarios._SELECTION_ELIGIBLE_SHORTS == {"NKE", "FLNC"}
    assert len(scenarios._SELECTION_BEARISH) == 15
    for symbol in ("GEV", "UNH", "NEE"):
        assert symbol not in scenarios._SELECTION_ELIGIBLE
        assert "R5" in scenarios._selection_blocked_by(symbol, "short")


def test_every_covered_mega_cap_is_admitted_which_is_why_it_is_not_scored():
    """AAPL, MSFT and NVDA all clear the desk's own rules — NVDA on a
    breakout the prompt says carries no reward:risk judgement whatsoever. A
    pass/fail familiarity check would therefore be failing models for taking
    trades the desk permits, so the check reports and does not score."""
    assert scenarios._SELECTION_FAMOUS_ELIGIBLE == {"AAPL", "MSFT", "NVDA"}
    assert "NVDA" in scenarios._SELECTION_BREAKOUT


def test_only_four_checks_carry_weight_and_familiarity_carries_none():
    checks = _by_name(scenarios._pm_selection_grade(_decision(_target("XLE"))))
    assert {name: c.weight for name, c in checks.items() if c.weight} == \
        _SCORING_WEIGHTS
    assert checks["familiarity_bias"].weight == 0.0
    assert sum(c.weight for c in checks.values()) == pytest.approx(_TOTAL_WEIGHT)


# --------------------------------------------------------------------------
# Discrimination
# --------------------------------------------------------------------------

def test_evidence_led_selection_scores_full_marks():
    """Four picks, every one admitted by the desk's own rules, including both
    admitted shorts."""
    decision = _decision(
        _target("XLE"),                       # buy/high, breakout
        _target("PFE"),                       # buy/medium, breakout
        _target("NKE", direction="short"),    # sell/medium, net +1
        _target("FLNC", direction="short"),   # sell/medium, net +1
    )
    checks = scenarios._pm_selection_grade(decision)
    assert _score(checks) == pytest.approx(1.0)
    assert all(c.passed for c in checks)


def test_a_book_of_refused_names_fails_the_selection_check():
    """Five real candidates the model was genuinely offered and the desk
    deterministically refuses — the §9.4 net-evidence rule on every one. This
    is the trap check now: a selector that reads ratings without reading
    agreement trips it immediately."""
    decision = _decision(
        _target("GEV", direction="short"),   # net -1
        _target("UNH", direction="short"),   # net  0
        _target("NEE", direction="short"),   # net  0
        _target("AVGO", direction="short"),  # net  0
        _target("AGX", direction="short"),   # net  0
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["selection_from_eligible_set"].passed is False
    assert checks["takes_an_eligible_short"].passed is False
    assert "R5" in checks["selection_from_eligible_set"].detail
    assert _score(list(checks.values())) == pytest.approx(0.20 / _TOTAL_WEIGHT)


def test_a_pick_in_a_direction_the_desk_does_not_admit_is_refused():
    """XLE is admitted LONG on a `buy` rating. Shorting it is not selecting
    from the admitted set, and the detail has to say which direction was."""
    checks = _by_name(scenarios._pm_selection_grade(
        _decision(_target("XLE", direction="short")),
    ))
    assert checks["selection_from_eligible_set"].passed is False
    assert "admitted long, not short" in checks["selection_from_eligible_set"].detail


def test_a_name_with_no_coverage_is_refused():
    checks = _by_name(scenarios._pm_selection_grade(_decision(_target("AMZN"))))
    assert checks["selection_from_eligible_set"].passed is False
    assert "no current technical coverage" in \
        checks["selection_from_eligible_set"].detail


def test_familiarity_is_reported_as_a_number_and_changes_no_score():
    """The owner asked for the diagnostic on every run. It must appear, and
    it must not move the score in either direction."""
    famous = _decision(
        _target("NVDA"), _target("AAPL"), _target("MSFT", risk=0.5),
    )
    checks = _by_name(scenarios._pm_selection_grade(famous))
    diagnostic = checks["familiarity_bias"]
    assert diagnostic.passed is False
    assert diagnostic.weight == 0.0
    assert "3/3" in diagnostic.detail and "100%" in diagnostic.detail
    # The passed-over admitted names have to be named, or the number is not
    # actionable to whoever reads the result file.
    assert "NKE" in diagnostic.detail and "XLE" in diagnostic.detail
    # All three are admitted, so the SCORED checks see a clean selection; the
    # only thing missing from this book is a short.
    assert checks["selection_from_eligible_set"].passed is True
    assert checks["takes_an_eligible_short"].passed is False
    assert _score(list(checks.values())) == pytest.approx(0.45 / _TOTAL_WEIGHT)


def test_familiarity_number_is_reported_even_when_it_is_zero():
    checks = _by_name(scenarios._pm_selection_grade(_decision(_target("XLE"))))
    diagnostic = checks["familiarity_bias"]
    assert diagnostic.passed is True
    assert "famous picks 0/1 (0%)" in diagnostic.detail


def test_doing_nothing_reproduces_the_live_failure_and_is_graded_as_failure():
    """run-64290730 executed zero trades against 38 actionable signals.
    Matching the live desk must not score."""
    checks = _by_name(scenarios._pm_selection_grade(_decision()))
    assert checks["parsed_and_grounded"].passed is True
    assert checks["opens_a_position"].passed is False
    # No vacuous credit: an empty book cannot pass a selection check by
    # having selected nothing.
    assert checks["selection_from_eligible_set"].passed is False
    assert checks["takes_an_eligible_short"].passed is False
    assert _score(list(checks.values())) == pytest.approx(0.10 / _TOTAL_WEIGHT)


def test_inaction_does_not_outscore_a_bad_but_real_book():
    """The vacuous-pass bug this guards against: inaction is the live desk's
    own failure and must sit at the bottom of the scale."""
    nothing = _score(scenarios._pm_selection_grade(_decision()))
    bad = _score(scenarios._pm_selection_grade(_decision(
        _target("GEV", direction="short"), _target("UNH", direction="short"),
    )))
    assert nothing < bad


def test_the_live_desks_own_targets_do_not_score_well():
    """The three long targets the production PM actually emitted, replayed
    against this grader. All three are names the desk admits — the failure
    was never which longs it chose, it was that it took no short and nothing
    reached the market."""
    decision = _decision(
        _target("XLE"),
        _target("CHPX", conviction="low"),
        _target("NVDA", catalyst="Strategic $3B investment into SB Energy."),
    )
    checks = _by_name(scenarios._pm_selection_grade(decision))
    assert checks["selection_from_eligible_set"].passed is True
    assert checks["takes_an_eligible_short"].passed is False   # the real gap
    assert _score(list(checks.values())) == pytest.approx(0.45 / _TOTAL_WEIGHT)


def test_unparsed_decision_scores_zero():
    checks = scenarios._pm_selection_grade(None)
    assert _score(checks) == pytest.approx(0.0)
    assert len(checks) == 1


# --------------------------------------------------------------------------
# What counts as a SELECTION
# --------------------------------------------------------------------------

def test_the_catalyst_door_still_admits_an_unmeasurable_payoff():
    """MSFT and TSM are the two names whose payoff the desk cannot measure
    and which enter only by citing a dated state-change row. That door is the
    one piece of the old sub-floor machinery item 1(d) left standing, so a
    pick that uses it must count as a pick from the admitted set."""
    checks = _by_name(scenarios._pm_selection_grade(
        _decision(_target("TSM"), _target("FLNC", direction="short")),
    ))
    assert checks["selection_from_eligible_set"].passed is True
    assert checks["takes_an_eligible_short"].passed is True


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
    # The MSFT trim is not a pick, so it is not in the familiarity count.
    assert "famous picks 0/1" in checks["familiarity_bias"].detail
    assert checks["selection_from_eligible_set"].passed is True


def test_adding_to_a_held_name_is_scored_as_selection():
    """The mirror of the trim: raising MSFT's weight IS choosing it."""
    checks = _by_name(scenarios._pm_selection_grade(
        _decision(_target("MSFT", risk=0.5)),
    ))
    assert checks["opens_a_position"].passed is True
    assert "famous picks 1/1" in checks["familiarity_bias"].detail
