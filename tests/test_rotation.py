"""Phase 14 — opportunity-cost rotation surfacing.

Hand-computed scenarios, `RankedCandidate` built directly, taking `ranked`
as already sorted best-first — exactly the contract `rank_verdicts` itself
guarantees and `test_analyst_verdict.py` already pins separately.

Two builders, because as of docs/WORK.md item 66 the module reads more than
`.symbol` and `.score`: `_rc` sets a score with no verdicts behind it (fine
for every path that never reaches the Tier 2 like-for-like check), and
`_rcv` builds real `AnalystVerdict`s and DERIVES the score from them with
`src/verdicts.py`'s own arithmetic, so a Tier 2 scenario's coverage and its
score cannot silently disagree.
"""
from src.models import AnalystVerdict, VerdictEvidence
from src.rotation import (
    ROTATION_MARGIN_PCT,
    evaluate_rotation_opportunity,
)
from src.verdicts import RankedCandidate, score_verdict, seat_weight

# The desk's own ratified floor (`STARTER_POSITION_RISK_PCT` /
# `RiskConfig.min_position_risk_pct`) — the exact number
# `_render_rotation_section` passes as `floor_pct`. Pinned here as a literal
# so a future change to that constant fails this test loudly instead of the
# scenarios below silently testing a different threshold than production.
FLOOR_PCT = 0.5


def _rc(symbol: str, score: float, direction: str = "bullish") -> RankedCandidate:
    return RankedCandidate(symbol=symbol, direction=direction, score=score)


def _verdict(symbol: str, seat: str, magnitude: float, conviction: str,
             direction: str = "bullish") -> AnalystVerdict:
    return AnalystVerdict(
        seat=seat, symbol=symbol, direction=direction, magnitude=magnitude,
        conviction=conviction, invalidation="the level breaks",
        evidence=[VerdictEvidence(label="close", value=100.0)],
    )


def _rcv(symbol: str, seats: dict[str, tuple[float, str]],
         direction: str = "bullish") -> RankedCandidate:
    """A candidate whose score is DERIVED from its own seat coverage.

    `seats` maps seat name -> (magnitude, conviction), and the score is
    `sum(seat_weight(seat) * score_verdict(verdict))` — the identical
    arithmetic `rank_verdicts` performs, so these scenarios exercise the
    real relationship between coverage and score rather than asserting one.
    """
    verdicts = [
        _verdict(symbol, seat, magnitude, conviction, direction)
        for seat, (magnitude, conviction) in seats.items()
    ]
    score = round(sum(seat_weight(v.seat) * score_verdict(v) for v in verdicts), 4)
    return RankedCandidate(
        symbol=symbol, direction=direction, score=score,
        verdicts=sorted(verdicts, key=lambda v: v.seat),
    )


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
    25% margin (0.9 * 1.25 = 1.125 <= 1.8).

    Both names are covered by the SAME single seat, so the item-66
    like-for-like sub-score is the whole score and the two checks coincide.
    """
    ranked = [
        _rcv("NEW", {"technical": (1.0, "medium")}),   # 1.2 * 1.5 = 1.8
        _rcv("OLD", {"technical": (0.75, "low")}),     # 1.2 * 0.75 = 0.9
    ]
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
    # Item 66: the comparison that was actually cleared is recorded.
    assert opp.shared_seats == ("technical",)
    assert opp.held_shared_score == 0.9
    assert opp.new_shared_score == 1.8


# --- (b) marginally-better new candidate does NOT trigger (respects margin) -

def test_marginal_edge_does_not_trigger_rotation():
    """NEW beats OLD but by less than the 25% margin: 0.9 * 1.25 = 1.125,
    and 1.08 falls short of that — no churn on a noise-level difference."""
    ranked = [
        _rcv("NEW", {"technical": (0.9, "low")}),      # 1.2 * 0.9  = 1.08
        _rcv("OLD", {"technical": (0.75, "low")}),     # 1.2 * 0.75 = 0.9
    ]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_exactly_at_the_margin_does_trigger():
    """The margin is a floor (>=), not a strict inequality: exactly 25%
    higher clears it. 0.9 * 1.25 = 1.125 exactly."""
    ranked = [
        _rcv("NEW", {"technical": (0.9375, "low")}),   # 1.2 * 0.9375 = 1.125
        _rcv("OLD", {"technical": (0.75, "low")}),     # 1.2 * 0.75   = 0.9
    ]
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
    ranked = [
        _rcv("NEW", {"technical": (0.9, "low")}),
        _rcv("OLD", {"technical": (0.75, "low")}),
    ]
    opp = evaluate_rotation_opportunity(
        ranked=ranked, blocked={"OLD": []}, held_symbols={"OLD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None  # falls through to the ranked-margin tier, which the
    # 1.08 vs 0.9 gap (same as the marginal test above) still does not clear


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


# --- (e) item 66: Tier 2 is coverage-neutral -------------------------------
#
# `rank_verdicts` became a weighted SUM on 2026-09-13 (correctly). A sum is
# not a rescaling of the average it replaced: the divisor it deletes is the
# name's own seat count, which differs per name. So two names' composites
# stopped being comparable term-for-term the moment their coverage differed
# — and Tier 2 compares exactly two such names. These pin that the margin
# must now also clear on the seats that scored BOTH names.

def test_coverage_decay_alone_does_not_rotate_a_held_name_out():
    """The item-66 case, end to end. HELD and NEW carry an IDENTICAL
    technical read — the strongest one that seat can give. NEW additionally
    has a live earnings filing and a confirmed flow; HELD's lapsed weeks
    ago. On the full composite that is 2.4 vs 4.0 and the 25% margin
    clears, so the pre-fix code would have surfaced a sale. On the one seat
    that scored both names it is 2.4 vs 2.4 — nothing about HELD is worse,
    so nothing is surfaced.
    """
    held = _rcv("HELD", {"technical": (1.0, "high")})
    new = _rcv("NEW", {
        "technical": (1.0, "high"),      # 1.2 * 2.0 = 2.4, identical to HELD
        "earnings": (0.0, "high"),       # 1.2 * 1.0 = 1.2, coverage HELD lost
        "smart_money": (0.0, "medium"),  # 0.8 * 0.5 = 0.4, coverage HELD lost
    })
    assert held.score == 2.4
    assert new.score == 4.0
    # The pre-fix comparison, spelled out so this test fails loudly if the
    # full-score margin ever stops being cleared by these inputs.
    assert new.score >= held.score * (1.0 + ROTATION_MARGIN_PCT)

    opp = evaluate_rotation_opportunity(
        ranked=[new, held], blocked={}, held_symbols={"HELD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_no_shared_scoring_seat_declines_rather_than_comparing():
    """Fail toward NOT selling. HELD is scored by earnings alone, NEW by
    technical alone; the full composites clear the margin (1.2 vs 2.4) but
    there is no seat that has an opinion on both, so there is nothing
    like-for-like to compare and Tier 2 declines."""
    held = _rcv("HELD", {"earnings": (0.0, "high")})     # 1.2
    new = _rcv("NEW", {"technical": (1.0, "high")})      # 2.4
    assert new.score >= held.score * (1.0 + ROTATION_MARGIN_PCT)

    opp = evaluate_rotation_opportunity(
        ranked=[new, held], blocked={}, held_symbols={"HELD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_a_real_like_for_like_gap_still_rotates_and_records_the_comparison():
    """The check is not a blanket refusal. HELD's own chart is weak where
    NEW's is strong, so the gap survives on the shared seat and the
    rotation is surfaced — with the seats and both sub-scores recorded, so
    the audit trail shows which comparison was actually cleared."""
    held = _rcv("HELD", {"technical": (0.75, "low")})    # 0.9
    new = _rcv("NEW", {
        "technical": (1.0, "high"),                      # 2.4
        "earnings": (0.0, "high"),                       # 1.2
    })
    opp = evaluate_rotation_opportunity(
        ranked=[new, held], blocked={}, held_symbols={"HELD"},
        headroom_pct=0.1, floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ranked_margin"
    assert opp.held_symbol == "HELD"
    assert opp.new_symbol == "NEW"
    assert opp.shared_seats == ("technical",)
    assert opp.held_shared_score == 0.9
    assert opp.new_shared_score == 2.4
    # And the sub-score is a strict partial of the real composite, never a
    # second scoring scheme: earnings is the only term dropped from NEW.
    assert round(opp.new_score - opp.new_shared_score, 4) == 1.2


def test_the_like_for_like_check_can_only_remove_rotations_never_add_one():
    """The property, not an example: the shared-seat test is a CONJUNCTION
    with the full-score test, so every surfaced ranked-margin rotation also
    clears the original comparison. Swept over a grid of coverage and
    strength combinations."""
    convictions = ("low", "medium", "high")
    magnitudes = (0.0, 0.5, 1.0)
    seat_sets = (
        ("technical",),
        ("technical", "earnings"),
        ("earnings", "smart_money"),
        ("technical", "earnings", "smart_money", "news", "macro"),
    )
    surfaced = 0
    for held_seats in seat_sets:
        for new_seats in seat_sets:
            for magnitude in magnitudes:
                for conviction in convictions:
                    held = _rcv(
                        "HELD", {s: (0.5, "low") for s in held_seats},
                    )
                    new = _rcv(
                        "NEW", {s: (magnitude, conviction) for s in new_seats},
                    )
                    opp = evaluate_rotation_opportunity(
                        ranked=[new, held], blocked={},
                        held_symbols={"HELD"},
                        headroom_pct=0.1, floor_pct=FLOOR_PCT,
                    )
                    if opp is None:
                        continue
                    surfaced += 1
                    assert opp.tier == "ranked_margin"
                    # (1) the original full-composite margin still holds
                    assert new.score >= held.score * (
                        1.0 + ROTATION_MARGIN_PCT
                    )
                    # (2) and so does the like-for-like one
                    assert opp.new_shared_score >= opp.held_shared_score * (
                        1.0 + ROTATION_MARGIN_PCT
                    )
                    # (3) which was computed over a genuinely shared set
                    assert set(opp.shared_seats) == (
                        set(held_seats) & set(new_seats)
                    )
    assert surfaced > 0, "grid surfaced nothing — it is not testing anything"


# --- (b) "no private execution path" regression guard ----------------------
#
# Phase 14b (2026-09-12) lets the desk ACT on the categorical tier behind
# `execution.rotation_enabled` — but only by appending an ordinary zero-size
# `TargetPosition` to the PM's plan in `DecisionStage`
# (`pipeline_stages._apply_rotation_execution`, tested in
# `tests/test_rotation_execute.py`). Everything downstream of that stays
# rotation-blind on purpose: `PortfolioConstructor` never imports or names
# this module, and `PortfolioDecision` / `TradeDecision` carry no rotation
# field, so a rotation close is built, risk-checked, reviewed and executed
# by EXACTLY the code a PM-authored close goes through. These tests pin that
# there is no second, rotation-specific route into order construction — if
# one is ever added, it starts by failing here.

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
    """The order-construction path must never even import `src.rotation` —
    a rotation close must reach it as a plain target, indistinguishable
    from a PM-authored one. A PR wiring rotation INTO construction would
    start here, and this fails the moment it does."""
    source = _module_source_path(portfolio_constructor_module).read_text()
    assert not _imports_rotation(source), (
        "src/portfolio_constructor.py must not import src.rotation — a "
        "rotation close is an ordinary zero-size target and the constructor "
        "must stay unable to tell it apart from a PM-authored one"
    )


def test_portfolio_constructor_construct_orders_never_references_rotation_by_name():
    """Belt-and-suspenders on top of the import check: even a local import
    inside a function body, or a same-module symbol literally named after
    rotation, would show up in the source text of the constructor's own
    module — catches the failure mode without depending on import style."""
    source = _module_source_path(portfolio_constructor_module).read_text()
    assert "rotation" not in source.lower(), (
        "src/portfolio_constructor.py source must not mention rotation at "
        "all — the constructor is the real order-execution path and must "
        "have no rotation-specific branch; a rotation close is built by the "
        "same code as any PM close"
    )


def test_portfolio_decision_and_trade_decision_carry_no_rotation_field():
    """The two data structures that actually reach execution/RM review must
    never carry a `RotationOpportunity` (or any rotation-named) field. The
    rotation close rides as an ordinary `TargetPosition`; its bookkeeping
    lives on `RunContext.rotation`, outside the decision objects, so the
    Risk Manager and the executor see a SELL like any other."""
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


def test_render_states_the_like_for_like_comparison_for_the_ranked_tier():
    """Item 66. When a ranked-margin rotation IS surfaced, the model is
    shown the coverage-neutral comparison that justified it, not only the
    two coverage-sensitive totals."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    result = PortfolioManagerAgent._render_rotation_section(
        ranked=[
            _rcv("NEW", {"technical": (1.0, "high"), "earnings": (0.0, "high")}),
            _rcv("HELD", {"technical": (0.75, "low")}),
        ],
        blocked={},
        held_symbols={"HELD"},
        existing_risk_pct={"HELD": 24.9},
        ceiling_pct=25.0,
    )
    assert "Like-for-like check" in result
    assert "technical" in result
    assert "0.90" in result and "2.40" in result
