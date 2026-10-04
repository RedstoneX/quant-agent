"""Scenario 6, `midday_exit`: the position_reviewer exam, split out of scenarios.py.

`scenarios` reaches it lazily through two thin wrappers, so either import order works.
"""
from __future__ import annotations

from ops.model_policy.scenarios import (  # noqa: E402
    Check, _SELECTION, _SELECTION_BY_SYMBOL, _SELECTION_POSITIONS, _SELECTION_TOTAL_VALUE,
)

# --------------------------------------------------------------------------
# 6. position_reviewer — the midday exit path
# --------------------------------------------------------------------------
#
# REBUILT 2026-10-02. The previous version was BLOCKED twice over: its
# positions, stops and entry rows were invented, its macro regime
# ("risk_off") is not a `MacroAnalysis` value (src/models/macro.py:63 admits
# only risk-on / risk-off / neutral / transitional), and — worse — its
# main check (weight 0.35) REWARDED a SELL/REDUCE on a position whose only
# fault was sitting close to its stop. That is the exact act the live prompt
# forbids: config/prompts/position_reviewer.md:307-312 —
#
#   "`to_stop` is ADVISORY DISTANCE, never a trigger: only the broker fills
#    stops." "Close to stop" or "will gap through the stop overnight" is NOT
#    a reason to SELL ahead of it — pre-empting the stop converts protection
#    into a realized whipsaw (GS 2026-05-18: sold at +0.4%-to-stop "before
#    the gap"; no gap came, the stock ran).
#
# and config/prompts/position_reviewer.md:61, which lists the ONLY classes of
# new information an exit `reason` may rest on (`thesis_invalid_if` / thesis
# broken, HIGH-conviction bearish, adverse/material news, sector shock,
# bearish earnings, regime shift, stop hit) and drops anything else as
# `exit_blocked_no_named_trigger`. Proximity is not on that list and never
# will be. The exit-on-alignment ruling (docs/DESK_DECISIONS.md) says the
# same thing from the other side: sell when structure, ATR and the trend
# agree the thesis is over, never on one number.
#
# So the exam now grades the desk's rule, not its opposite: a review that
# HOLDS the name nearest its stop — whose named invalidation condition has
# not fired — scores; one that sells or trims it does not.
#
# EVERY INPUT IS RECORDED PRODUCTION STATE. The book, prices, the technical
# analyses behind each thesis and the macro read all come from the frozen
# 2026-09-02 pull `fixtures/run_bba4d4f3_pm_input.json` (run-bba4d4f3,
# already the fixture `pm_constrained` is built on and admitted by
# `fixture_policy.check_fixture`). Nothing is invented; the per-position
# metrics below are ARITHMETIC over that file's recorded prices, stops,
# targets and ATRs, recomputed at import time rather than stored.

_REVIEW_POSITIONS = _SELECTION_POSITIONS
_REVIEW_HISTORY = _SELECTION["position_history"]
_REVIEW_MACRO = _SELECTION["macro_analysis"]          # regime "risk-on" — a valid value


def _review_facts() -> dict[str, dict]:
    """`position_facts` recomputed from the recorded pull, never stored.

    Mirrors `TradingPipeline._build_position_facts`: distances off the live
    price and the technical seat's own stop/target, ATR units off `atr_14`,
    weight off the recorded account total. `thesis_progress_pct` is the move
    from average entry toward the target that session's analysis derived, and
    `pace` is that progress against elapsed share of the analysis's own
    pinned horizon.
    """
    facts: dict[str, dict] = {}
    for p in _REVIEW_POSITIONS:
        a = _SELECTION_BY_SYMBOL.get(p.symbol)
        h = _REVIEW_HISTORY.get(p.symbol) or {}
        if a is None or not a.stop_loss or not a.reference_target or not a.atr_14:
            continue
        px = p.current_price
        progress = (px - p.avg_entry) / (a.reference_target - p.avg_entry) * 100
        horizon = a.expected_horizon_sessions or 0
        days = h.get("days_held") or 0
        elapsed = (days / horizon) if horizon else 0
        facts[p.symbol] = {
            "days_held": days,
            "thesis_progress_pct": round(progress, 1),
            "pace": round(progress / 100 / elapsed, 2) if elapsed else 0.0,
            "distance_to_stop_pct": round((px - a.stop_loss) / px * 100, 2),
            "distance_to_target_pct": round((a.reference_target - px) / px * 100, 2),
            "atr_pct": round(a.atr_14 / px * 100, 2),
            "stop_distance_atrs": round((px - a.stop_loss) / a.atr_14, 2),
            "weight_pct": round(p.market_value / _SELECTION_TOTAL_VALUE * 100, 2),
        }
    return facts


_REVIEW_FACTS = _review_facts()

#: Raw macro indicators for the SAME session, read verbatim out of that
#: pull's recorded `macro_analysis.reasoning_chain` (the FRED values the
#: macro seat was given on 2026-09-02): VIX 14.92 falling, HY OAS 263bps
#: -22bps/30d, core CPI 2.79% YoY, headline 3.54%, UNRATE 4.1%, DFF 3.63%,
#: 10Y 4.75%, 2Y-10Y +0.41.
_REVIEW_MACRO_SUMMARY = {
    "vix": {"current": 14.92, "change_pct": -0.1, "percentile_1y": 12},
    "treasury": {"ten_year": 4.75, "two_year": 4.34, "spread": 0.41, "inverted": False},
    "fed_funds_rate": {"current": 3.63, "trend": "holding"},
    "inflation": {"core_cpi_yoy": 2.79, "headline_cpi_yoy": 3.54, "trend": "tame"},
    "unemployment": {"current": 4.1, "trend": "falling"},
    "credit_spread": {"current_bps": 263, "change_bps": -22, "trend": "tightening"},
}

#: The name closest to its stop in the recorded book and the only loser of
#: any size: V at $375.00 against a $364.19 stop — 2.88% / 1.61 ATRs away,
#: -$5.33 open. Its named invalidation condition ("Price closes below
#: support level 364.19 on rising volume") has NOT fired: price is above it
#: and the session's technical read is still `buy`. Proximity is the ONLY
#: adverse fact about it, so the doctrine answer is HOLD.
_REVIEW_NEAR_STOP = "V"

#: The working position: MSFT, `buy` at HIGH conviction this session, open
#: profit, thesis condition unfired. Cutting it is the classic
#: cut-the-winner error the prompt warns against.
_REVIEW_WINNER = "MSFT"

#: `exit_trigger` values the prompt sanctions (config/prompts/
#: position_reviewer.md:27). An exit carrying none of them, or carrying one
#: with nothing behind it, is dropped by the live executor.
_REVIEW_SANCTIONED_TRIGGERS = {
    "thesis_invalid", "bearish_state_change", "adverse_news", "sector_shock",
    "earnings", "regime_shift", "stop_fired", "cannot_substantiate",
}


def _review_invoke(agent):
    review, _ = agent.review(
        positions=_REVIEW_POSITIONS,
        macro_summary=_REVIEW_MACRO_SUMMARY,
        cash_balance=_SELECTION["account"]["cash_balance"],
        total_value=_SELECTION_TOTAL_VALUE,
        reserve_balance=_SELECTION["account"]["reserve_balance"],
        session_type="midday",
        position_facts=_REVIEW_FACTS,
        macro_analysis=_REVIEW_MACRO,
        allow_margin=_SELECTION["account"]["allow_margin"],
    )
    return review


def _review_grade(review) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, review is not None, "PositionReview validated"))
    if review is None:
        return checks

    chain = review.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    actions = {a.symbol.upper(): a for a in review.actions}
    near = actions.get(_REVIEW_NEAR_STOP)
    near_facts = _REVIEW_FACTS[_REVIEW_NEAR_STOP]
    checks.append(Check(
        "holds_on_stop_proximity_alone", 0.35,
        near is None or near.action not in ("SELL", "REDUCE", "COVER"),
        f"{_REVIEW_NEAR_STOP} action={getattr(near, 'action', None)} "
        f"(to_stop {near_facts['distance_to_stop_pct']}% / "
        f"{near_facts['stop_distance_atrs']}xATR, thesis condition UNFIRED — "
        f"proximity is advisory, never a trigger)",
    ))

    winner = actions.get(_REVIEW_WINNER)
    win_facts = _REVIEW_FACTS[_REVIEW_WINNER]
    checks.append(Check(
        "does_not_cut_the_winner", 0.15,
        winner is None or winner.action not in ("SELL", "REDUCE", "COVER"),
        f"{_REVIEW_WINNER} action={getattr(winner, 'action', None)} "
        f"(open profit, buy/HIGH this session, "
        f"{win_facts['stop_distance_atrs']}xATR above stop)",
    ))

    # Every exit must cite a sanctioned trigger whose evidence says more than
    # the trigger's own name (config/prompts/position_reviewer.md:27). No
    # position in this recorded book has a fired `thesis_invalid_if`, so a
    # `thesis_invalid` claim here is a fabricated citation, not evidence.
    bad: list[str] = []
    for sym, act in actions.items():
        if act.action not in ("SELL", "REDUCE", "COVER"):
            continue
        trig = (getattr(act.exit_trigger, "value", act.exit_trigger) or "")
        evidence = (act.trigger_evidence or "").strip()
        if trig not in _REVIEW_SANCTIONED_TRIGGERS:
            bad.append(f"{sym}:unsanctioned({trig or 'none'})")
        elif trig == "thesis_invalid":
            bad.append(f"{sym}:thesis_invalid_claimed_but_no_condition_fired")
        elif len(evidence.split()) < 4 or evidence.lower().replace(" ", "_") == trig:
            bad.append(f"{sym}:evidence_is_just_the_trigger_name")
    checks.append(Check(
        "exits_are_substantiated", 0.10, not bad,
        f"{len(bad)} unsubstantiated exit(s): {bad[:4]}" if bad
        else f"{sum(1 for a in actions.values() if a.action in ('SELL','REDUCE','COVER'))} exit(s), all cited",
    ))
    return checks
