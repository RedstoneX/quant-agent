"""The `pm_public_day` grader, and the fixture it grades against.

Mirrors `tests/test_pm_selection_scenario.py`'s shape: never calls a model.
Drives `_pm_public_day_grade` with hand-built `PortfolioDecision` objects and
checks the fixture itself against `fixture_policy` and against a drift guard
on the live PM builder (`src/pipeline_stages.py::decide()` call site).
"""
from __future__ import annotations

import importlib
import json

from src.models import PortfolioDecision, ReasoningChain, TargetPosition

scenarios = importlib.import_module("ops.model_policy.scenarios")
fixture_policy = importlib.import_module("ops.model_policy.fixture_policy")


def _chain(**overrides) -> ReasoningChain:
    fields = dict(
        macro_filter="Regime and target invested noted.",
        news_check="Checked against today's briefing.",
        earnings_check="No JUST FILED name increased.",
        signal_conflicts="Adjudicated per symbol.",
        sizing_logic="Sized to conviction and evidence.",
        portfolio_balance="No sector above the cap.",
        cash_target="Closing the deployment gap.",
        continuity_check="No prior sessions — first day for this account.",
        premortem_check="Strongest case against the largest new position.",
        macro_audit="No logic error found in the macro seat's reasoning.",
    )
    fields.update(overrides)
    return ReasoningChain(**fields)


def _decision(*targets: TargetPosition, chain: ReasoningChain | None = None) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=chain or _chain(), targets=list(targets),
        portfolio_view="Test book.",
    )


def _target(symbol: str, *, risk: float = 1.0) -> TargetPosition:
    return TargetPosition(
        symbol=symbol, direction="long", risk_allocation_pct=risk,
        conviction="medium", thesis=f"{symbol} thesis for the grader.",
        thesis_invalid_if="Closes through the level.",
    )


def _score(checks) -> float:
    total = sum(c.weight for c in checks) or 1.0
    return sum(c.weight for c in checks if c.passed) / total


def _by_name(checks) -> dict:
    return {c.name: c for c in checks}


# --------------------------------------------------------------------------
# The fixture: policy-admissible, no desk data, and matches the doc'd shape
# --------------------------------------------------------------------------

def test_fixture_is_admissible_under_fixture_policy():
    verdict = fixture_policy.check_fixture(
        fixture_policy.FIXTURES_DIR / scenarios._PM_PUBLIC_DAY_FIXTURE
    )
    assert verdict.admissible, verdict.problems


def test_scenario_is_not_refused():
    assert scenarios.refusal_reason(scenarios.SCENARIOS_BY_KEY["pm_public_day"]) is None


def test_fixture_matches_real_day_scale():
    """2026-09-14 OWNER RULING: real-day scale and shape, not a 5/2
    hand-pick. 62/47/15 are MEASURED against the live-derived 101-symbol
    universe (config/settings.yaml:1192) and its live prefilter
    (`TradingPipeline._has_actionable_signal_fn`, src/pipeline.py:2694) on
    2026-09-14 — see test_fixture_scale_matches_live_derived_values below
    for the guard against this silently shrinking again."""
    manifest, analyses, *_ = scenarios._pm_public_day_inputs()
    actionable = [a for a in analyses if a.rating != "neutral"]
    neutral = [a for a in analyses if a.rating == "neutral"]
    assert len(actionable) == 15
    assert len(neutral) == 47
    assert len(manifest["earnings_analyses"]) == 49


def test_eligible_set_matches_the_live_candidate_eligibility_gate():
    """The admitted set here IS `PortfolioManagerAgent.candidate_eligibility`
    — not a second opinion of it. If this fixture's shape ever changes such
    that the two diverge, the scenario has silently grown its own rules."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    _, analyses, *_ = scenarios._pm_public_day_inputs()
    eligible = scenarios._pm_public_day_eligible_set(analyses)
    evidence_registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses, positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, smart_money_findings=[],
    )
    replay = PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses, evidence_registry=evidence_registry,
        allowed_buy_symbols={a.symbol.upper() for a in analyses},
        active_state_changes="",
    )
    assert eligible == replay
    admitted = {s for s, why in eligible.items() if not why}
    assert admitted == {
        "AAPL", "AGX", "BRK-B", "COP", "CVX", "EQNR", "JPM", "MU",
        "NEE", "NET", "OKLO", "ONDS", "OXY", "TSM", "ZS",
    }
    assert eligible["UNH"] and eligible["AMZN"]  # neutral, refused


def test_fixture_scale_matches_live_derived_values():
    """OWNER RULING (2026-09-14): the exam must be derived from TODAY's live
    code/config, not a fixed number that can quietly drift from it. This
    reads the universe size and the earnings/insider windows straight out
    of the live config and code paths, and checks the raw-fact fixtures'
    own `_exam` blocks against them — so a future edit to
    config/settings.yaml's `trading.universe` or `smart_money.lookback_days`,
    or to `EarningsDataProvider`'s default `lookback_days`, fails this test
    instead of silently going unnoticed."""
    import yaml

    from src.data.earnings import EarningsDataProvider

    settings = yaml.safe_load(
        (fixture_policy.FIXTURES_DIR / ".." / ".." / ".." / "config" / "settings.yaml")
        .resolve().read_text()
    )
    universe = settings["trading"]["universe"]
    assert len(universe) == 101

    tech_manifest = json.loads(
        (fixture_policy.FIXTURES_DIR / "yf_daily_bars_pm_public_day_2026-09-14.json").read_text()
    )
    assert tech_manifest["_exam"]["universe_size"] == len(universe)
    assert tech_manifest["_exam"]["lookback_days"] == settings["trading"]["lookback_days"]

    earnings_manifest = json.loads(
        (fixture_policy.FIXTURES_DIR / "sec_10q10k_pm_public_day_2026-09-14.json").read_text()
    )
    assert earnings_manifest["_exam"]["lookback_days"] == EarningsDataProvider().lookback_days
    assert earnings_manifest["_exam"]["universe_size"] == len(universe)

    form4_manifest = json.loads(
        (fixture_policy.FIXTURES_DIR / "sec_form4_pm_public_day_2026-09-14.json").read_text()
    )
    assert form4_manifest["_exam"]["configured_lookback_days"] == settings["smart_money"]["lookback_days"]


# --------------------------------------------------------------------------
# The grader discriminates
# --------------------------------------------------------------------------

def test_none_decision_scores_zero():
    checks = scenarios._pm_public_day_grade(None)
    assert _score(checks) == 0.0


def test_eligible_only_picks_with_full_reasoning_chain_scores_1():
    decision = _decision(_target("JPM"), _target("MU"))
    checks = scenarios._pm_public_day_grade(decision)
    assert _score(checks) == 1.0, [c.detail for c in checks if not c.passed]


def test_picking_a_neutral_name_fails_the_eligibility_check():
    decision = _decision(_target("JPM"), _target("UNH"))
    checks = _by_name(scenarios._pm_public_day_grade(decision))
    assert checks["opens_only_from_eligible_set"].passed is False
    assert "UNH" in checks["opens_only_from_eligible_set"].detail


def test_missing_reasoning_chain_field_fails_the_schema_check():
    decision = _decision(_target("JPM"), chain=_chain(macro_audit=""))
    checks = _by_name(scenarios._pm_public_day_grade(decision))
    assert checks["reasoning_chain_all_ten_fields"].passed is False
    assert "macro_audit" in checks["reasoning_chain_all_ten_fields"].detail


def test_empty_book_still_passes_eligibility_and_schema():
    """An empty book selects from nothing, so it cannot fail on selection —
    same reasoning as pm_selection's own empty-book case."""
    decision = _decision()
    checks = _by_name(scenarios._pm_public_day_grade(decision))
    assert checks["opens_only_from_eligible_set"].passed is True
    assert checks["reasoning_chain_all_ten_fields"].passed is True


# --------------------------------------------------------------------------
# Drift guard against the live PM input builder
# --------------------------------------------------------------------------

def test_decide_signature_still_accepts_every_argument_this_scenario_passes():
    """`_pm_public_day_invoke` calls a fixed subset of
    `PortfolioManagerAgent.decide()`'s keyword arguments. If a future change
    renames or removes one of them, this fails loudly here rather than as a
    silent TypeError the next time someone runs the benchmark.
    """
    import inspect

    from src.agents.portfolio_manager import PortfolioManagerAgent

    params = set(inspect.signature(PortfolioManagerAgent.decide).parameters)
    used = {
        "analyses", "positions", "macro_analysis", "cash_balance",
        "reserve_balance", "total_value", "news_intel", "earnings_analyses",
        "smart_money_findings", "allow_margin", "session_type",
        "allowed_buy_symbols", "transient_admitted_symbols",
    }
    assert used <= params, used - params


def test_earnings_rows_use_the_live_pipeline_wrapper_shape():
    """2026-09-15: the fixture stored flat analyses; PortfolioManagerAgent
    skips any row without a dict `analysis` (portfolio_manager.py
    `_earnings_stance_rows` and the prompt's earnings section), so the PM
    test ran with zero earnings evidence. Every row must carry the wrapper."""
    import json
    from pathlib import Path
    fx = Path(__file__).resolve().parents[1] / "ops/model_policy/fixtures/pm_public_day_pm_input.json"
    rows = json.loads(fx.read_text())["earnings_analyses"]
    assert rows, "fixture has no earnings rows"
    for row in rows:
        assert isinstance(row.get("analysis"), dict), row.get("symbol")
        assert row.get("symbol") and row.get("filing_date")
