"""Phase 14 — opportunity-cost rotation surfacing.

Hand-computed scenarios, `RankedCandidate` built directly (bypassing
`AnalystVerdict`/`rank_verdicts` — this module only ever reads `.symbol`
and `.score`, and takes `ranked` as already sorted best-first, exactly the
contract `rank_verdicts` itself guarantees and `test_analyst_verdict.py`
already pins separately).
"""
from src.rotation import (
    ROTATION_MARGIN_PCT,
    evaluate_rotation_opportunity,
)
from src.verdicts import RankedCandidate

# The desk's own ratified floor (`STARTER_POSITION_RISK_PCT` /
# `RiskConfig.min_position_risk_pct`) — the exact number
# `_render_rotation_section` passes as `floor_pct`. Pinned here as a literal
# so a future change to that constant fails this test loudly instead of the
# scenarios below silently testing a different threshold than production.
FLOOR_PCT = 0.5


def _rc(symbol: str, score: float, direction: str = "bullish") -> RankedCandidate:
    return RankedCandidate(symbol=symbol, direction=direction, score=score)


# --- (a) clearly-stronger new candidate vs clearly-weaker/stale holding ----

def test_stronger_new_candidate_rotates_out_a_stale_ineligible_holding():
    """STALE is caught categorically: OLD fails the desk's own eligibility
    gates outright (R4/R5), so no margin is needed at all — it would not be
    bought today by the identical rule a new buy must clear."""
    # OLD is held but its own eligibility row carries real blocking reasons
    # — it never enters `ranked` at all, because `rank_candidates` only
    # ranks ELIGIBLE names. The categorical branch reads only its presence
    # (with reasons) in `blocked`, never a score.
    ranked = [_rc("NEW", 1.8)]
    blocked = {"OLD": ["R4 R/R 0.80 under the 1.50 floor and no current state-change row names it"]}
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked=blocked, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ineligible_hold"
    assert opp.new_symbol == "NEW"
    assert opp.held_symbol == "OLD"
    assert opp.held_score is None
    assert opp.reasons


def test_stronger_new_candidate_rotates_out_a_weak_but_still_eligible_holding():
    """Both sides eligible: OLD ranks last among held names, NEW clears the
    25% margin (0.9 * 1.25 = 1.125 <= 1.8)."""
    ranked = [_rc("NEW", 1.8), _rc("OLD", 0.9)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ranked_margin"
    assert opp.new_symbol == "NEW"
    assert opp.new_score == 1.8
    assert opp.held_symbol == "OLD"
    assert opp.held_score == 0.9
    assert opp.margin_pct == ROTATION_MARGIN_PCT


# --- (b) marginally-better new candidate does NOT trigger (respects margin) -

def test_marginal_edge_does_not_trigger_rotation():
    """NEW beats OLD but by less than the 25% margin: 0.9 * 1.25 = 1.125,
    and 1.10 falls short of that — no churn on a noise-level difference."""
    ranked = [_rc("NEW", 1.10), _rc("OLD", 0.9)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_exactly_at_the_margin_does_trigger():
    """The margin is a floor (>=), not a strict inequality: exactly 25%
    higher clears it. 0.9 * 1.25 = 1.125 exactly."""
    ranked = [_rc("NEW", 1.125), _rc("OLD", 0.9)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ranked_margin"


# --- (c) capital NOT constrained -> no comparison runs at all --------------

def test_real_headroom_means_no_rotation_check_at_all():
    """Even an enormous, obviously-qualifying gap is not surfaced when the
    book has real room left — refusal-driven comparison only activates when
    it is actually needed, never as a standing "could we do better" nudge."""
    ranked = [_rc("NEW", 5.0), _rc("OLD", 0.1)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=10.0, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_headroom_exactly_at_the_floor_is_not_constrained():
    """`headroom_pct >= floor_pct` reads as "a new floor-sized idea still
    fits" — the boundary itself is not yet a constraint."""
    ranked = [_rc("NEW", 5.0), _rc("OLD", 0.1)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=FLOOR_PCT, floor_pct=FLOOR_PCT,
    )
    assert opp is None


# --- (d) nothing to recommend -> no change to existing behaviour ----------

def test_no_new_candidate_means_nothing_to_recommend():
    ranked = [_rc("OLD", 0.9)]  # only a held name is even ranked
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_no_held_position_ranked_or_blocked_means_nothing_to_compare():
    """Constrained capital, a strong new idea, but nothing held to weigh it
    against (e.g. an empty book) — there is no rotation to propose."""
    ranked = [_rc("NEW", 1.8)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols=set(),
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_empty_blocked_reasons_are_not_treated_as_a_blocking_row():
    """A `blocked` dict may carry a symbol with an empty reasons list (this
    codebase's own "empty list = eligible" convention, `candidate_eligibility`
    docstring) — that must not be misread as a categorical hit."""
    ranked = [_rc("NEW", 1.10), _rc("OLD", 0.9)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={"OLD": []}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None  # falls through to the ranked-margin tier, which the
    # 1.10 vs 0.9 gap (same as the marginal test above) still does not clear


def test_multiple_ineligible_holdings_pick_the_worse_one_deterministically():
    """More blocking reasons is the worse candidate to keep; ties break
    alphabetically, matching this module's own documented tie-break."""
    ranked = [_rc("NEW", 1.8)]
    blocked = {
        "AAA": ["R4 one reason"],
        "BBB": ["R4 one reason", "R5 net evidence -1 if long — no rung"],
    }
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked=blocked, held_symbols={"AAA", "BBB"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.held_symbol == "BBB"


def test_a_held_name_that_is_itself_the_best_ranked_candidate_is_not_compared_against_itself():
    """The strongest name overall happens to already be held — nothing to
    rotate into, since the new-candidate pool excludes anything held."""
    ranked = [_rc("HELD", 5.0), _rc("NEW", 1.0)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"HELD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    # NEW (1.0) vs HELD (5.0): 5.0 * 1.25 = 6.25 > 1.0, well under margin.
    assert opp is None


# --- (b) information-only regression guard ---------------------------------
#
# `RotationOpportunity` is currently wired into exactly one place:
# `PortfolioManagerAgent._render_rotation_section`, which folds it into
# plain prompt TEXT for the LLM to read — never into `PortfolioDecision`,
# never into `TradeDecision`/order construction, and `PortfolioConstructor`
# (the actual execution path, `src/portfolio_constructor.py`) never imports
# this module at all. That is the whole "information only, never automatic"
# property this feature is supposed to have. The tests above only exercise
# `evaluate_rotation_opportunity()` in isolation and would keep passing even
# if someone later wired `RotationOpportunity` into an order-construction
# path — these guard against exactly that regression.

import ast
import inspect
import pathlib

import src.portfolio_constructor as portfolio_constructor_module
from src.models import PortfolioDecision, TradeDecision


def _module_source_path(module) -> pathlib.Path:
    return pathlib.Path(inspect.getsourcefile(module))


def _imports_rotation(source: str) -> bool:
    """True if `source` imports anything from/as `src.rotation` or
    `rotation`, via any `import`/`from ... import` form (not just a
    textual grep, so a rename or an aliased import is still caught)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == "rotation" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[-1] == "rotation":
                return True
    return False


def test_portfolio_constructor_module_never_imports_rotation():
    """The execution path must never even import `src.rotation` — a future
    PR wiring rotation into order construction would start here, and this
    fails the moment it does, independent of what it does with the import."""
    source = _module_source_path(portfolio_constructor_module).read_text()
    assert not _imports_rotation(source), (
        "src/portfolio_constructor.py must not import src.rotation — "
        "RotationOpportunity is information for the PM's prompt only and "
        "must never reach the order-construction path"
    )


def test_portfolio_constructor_construct_orders_never_references_rotation_by_name():
    """Belt-and-suspenders on top of the import check: even a local import
    inside a function body, or a same-module symbol literally named after
    rotation, would show up in the source text of the constructor's own
    module — catches the failure mode without depending on import style."""
    source = _module_source_path(portfolio_constructor_module).read_text()
    assert "rotation" not in source.lower(), (
        "src/portfolio_constructor.py source must not mention rotation at "
        "all — the constructor is the real order-execution path, and this "
        "feature is documented as information-only, never automatic"
    )


def test_portfolio_decision_and_trade_decision_carry_no_rotation_field():
    """The two data structures that actually reach execution/RM review must
    never carry a `RotationOpportunity` (or any rotation-named) field. If
    someone ever adds one to make rotation "automatic", this fails instead
    of silently starting to flow through the decision pipeline."""
    for model in (PortfolioDecision, TradeDecision):
        field_names = set(model.model_fields.keys())
        rotation_fields = {f for f in field_names if "rotation" in f.lower()}
        assert not rotation_fields, (
            f"{model.__name__} must not carry a rotation field, found "
            f"{rotation_fields} — RotationOpportunity is prompt-text only"
        )


def test_render_rotation_section_returns_plain_text_not_structured_data():
    """`_render_rotation_section` is the ONE place `RotationOpportunity`
    is consumed. Its contract must stay "renders to a prompt string" — not
    "returns something that could be attached to a decision object". If it
    is ever changed to return the `RotationOpportunity`/a dict/anything
    structured (the shape a future "wire it into the decision" change would
    need), this fails immediately."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    result = PortfolioManagerAgent._render_rotation_section(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        existing_risk_pct=None,
        ceiling_pct=25.0,
    )
    assert isinstance(result, str)
    # `None` book-risk telemetry is the fail-open/skip path — still a str.
    assert "Opportunity Rotation" in result
