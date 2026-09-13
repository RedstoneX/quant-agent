"""PM TEST GATE item 7 — the decision seat gets bounded conclusions, not nulls.

These tests render the FROZEN `run_64290730` fixture through the real
`PortfolioManagerAgent.build_user_message` — the same fixture and the same
method `ops/model_policy/scenarios.py` grades model selection against — and
assert the properties the redesign has to hold, not a character count someone
liked the look of.

The two shape changes under test:

  * an earnings filing the seat read WITHOUT reaching a direction is one
    roll-up line naming the symbol, its form, its date and its conviction,
    instead of a four-line verdict block whose direction, thesis and
    falsifier are all absent;
  * a `neutral` technical read — which by construction has no entry, stop or
    target, so `risk_reward` is None — renders the analyst's one-sentence
    conclusion without the three `None` fields and the
    "Invalid if: (not specified)" line.

What the tests are really guarding is the constraint that makes the change
safe: coverage must never become invisible. A reader of the prompt has to be
able to tell "read, concluded nothing" from "never read" for every symbol,
and every dissenting or low-conviction stance has to keep reaching the seat
through the registry and the net-agreement arithmetic untouched.
"""

from __future__ import annotations

import importlib
import re

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent

scenarios = importlib.import_module("ops.model_policy.scenarios")


# The marker the old null block printed, verbatim. Its ABSENCE is the point:
# a line that says the analyst disclosed nothing is not evidence, and four
# lines per filing of it was 16.7% of everything the decision seat read.
_NULL_FALSIFIER = "Invalidated if: not disclosed by the analyst"


@pytest.fixture(scope="module")
def rendered() -> str:
    sel = scenarios._SELECTION
    account = sel["account"]
    memory = sel["memory"]
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    return agent.build_user_message(
        analyses=scenarios._SELECTION_ANALYSES,
        positions=scenarios._SELECTION_POSITIONS,
        macro_analysis=sel["macro_analysis"],
        cash_balance=account["cash_balance"],
        reserve_balance=account["reserve_balance"],
        total_value=account["total_value"],
        news_intel=scenarios._SELECTION_NEWS,
        earnings_analyses=sel["earnings_analyses"],
        recent_performance=sel["recent_performance"],
        position_history=sel["position_history"],
        yesterday_insights=sel["yesterday_insights"],
        weekly_narrative=memory["weekly_narrative"],
        macro_trajectory=memory["macro_trajectory"],
        active_state_changes=memory["active_state_changes"],
        rm_recent_verdicts=memory["rm_recent_verdicts"],
        pm_recent_decisions=memory["pm_recent_decisions"],
        projected_portfolio=memory["projected_portfolio"],
        calibration_note=memory["calibration_note"],
        recent_missed_lessons=memory["recent_missed_lessons"],
        recent_loss_pits=memory["recent_loss_pits"],
        allow_margin=account["allow_margin"],
        session_type=account["session_type"],
        allowed_buy_symbols=set(sel["allowed_buy_symbols"]),
        transient_admitted_symbols=set(sel["transient_admitted_symbols"]),
    )


def _section(text: str, heading: str) -> str:
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        if part.startswith("## " + heading):
            return part
    raise AssertionError(f"section not found: {heading}")


def _neutral_earnings_symbols() -> set[str]:
    """Symbols whose earnings read collapses to no direction, from the data.

    Derived the same way the renderer derives it — `_collapse_stances` over
    the seat's own `sentiment` — so the test cannot drift from the code by
    hardcoding a list someone typed once.
    """
    out = set()
    for item in scenarios._SELECTION["earnings_analyses"]:
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            continue
        impl = analysis.get("investment_implications") or {}
        stance = PortfolioManagerAgent._collapse_stances([impl.get("sentiment")])
        if stance in (None, "neutral"):
            out.add(str(item["symbol"]).strip().upper())
    return out


def test_fixture_still_contains_the_no_call_majority_this_is_measuring():
    """Guard the premise. If a fixture edit removes the no-call filings, every
    assertion below would pass vacuously and prove nothing."""
    neutral = _neutral_earnings_symbols()
    analysed = [
        item for item in scenarios._SELECTION["earnings_analyses"]
        if isinstance(item.get("analysis"), dict)
    ]
    assert len(analysed) == 65
    assert len(neutral) == 38


def test_no_call_filings_are_not_rendered_as_empty_verdict_blocks(rendered):
    earnings = _section(rendered, "Earnings Analysis")
    assert _NULL_FALSIFIER not in earnings


def test_every_no_call_symbol_is_still_named_in_the_prompt(rendered):
    """The load-bearing one. Shortening the input must not make coverage
    invisible — a symbol the seat read and declined to call has to stay
    distinguishable from a symbol nobody looked at."""
    earnings = _section(rendered, "Earnings Analysis")
    rollup = earnings[earnings.index("### Read, no call"):]
    listed = set(re.findall(r"^- ([A-Z][A-Z0-9.\-]*) \| ", rollup, re.M))
    assert listed == _neutral_earnings_symbols()


def test_no_call_lines_carry_form_date_and_conviction(rendered):
    earnings = _section(rendered, "Earnings Analysis")
    rollup = earnings[earnings.index("### Read, no call"):]
    for line in rollup.splitlines():
        if not line.startswith("- "):
            continue
        # symbol | FORM (YYYY-MM-DD) | conviction X [provenance]
        assert re.match(
            r"^- [A-Z][A-Z0-9.\-]* \| \S+ \(\d{4}-\d{2}-\d{2}\) \| conviction \S+",
            line,
        ), line


def test_directional_filings_keep_the_full_bounded_verdict(rendered):
    """The roll-up must not swallow a real call. A directional read still
    ships its call, conviction, thesis, falsifier and audit pointer."""
    earnings = _section(rendered, "Earnings Analysis")
    blocks = [
        b for b in re.split(r"\n(?=### )", earnings)
        if b.startswith("### ")
        and not b.startswith("### Read, no call")
        # The two JUST FILED placeholders are a third case: a filing dropped
        # today with the seat still working. They have no analysis at all, so
        # they are neither a call nor a no-call, and they keep their own block.
        and "JUST FILED" not in b.splitlines()[0]
    ]
    assert len(blocks) == 65 - 38
    for block in blocks:
        assert "- Call: " in block
        assert "- Thesis: " in block
        assert "- Invalidated if: " in block
        assert "- Full 8-field extraction" in block
        assert re.search(r"- Call: (bullish|bearish|mixed) ", block), block


def test_a_mixed_read_is_never_rolled_up():
    """A conclusion that hides a disagreement is worse than the raw prose.
    `mixed` is a split between sources, not an absence of one, so it must
    keep its full block even though it is not a clean direction."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    assert agent._collapse_stances(["mixed"]) == "mixed"
    assert agent._collapse_stances(["bullish", "bearish"]) == "mixed"
    # And the renderer's partition only rolls up None/neutral.
    assert agent._collapse_stances(["neutral"]) == "neutral"
    assert agent._collapse_stances([None]) is None
    assert agent._collapse_stances(["bullish"]) == "bullish"


def test_neutral_technical_reads_drop_the_none_fields_not_the_conclusion(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    neutral_lines = [
        ln for ln in tech.splitlines() if re.match(r"^- \S+: neutral ", ln)
    ]
    assert neutral_lines, "fixture should carry neutral technical reads"
    for line in neutral_lines:
        assert "Entry: None" not in line
        assert "Stop: None" not in line
        assert "Target: None" not in line
        assert "R/R n/a" not in line
        assert "not sizeable this session" in line


def test_every_analysed_symbol_still_appears_in_the_technical_section(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    listed = set(re.findall(r"^- ([A-Z][A-Z0-9.\-]*): ", tech, re.M))
    assert listed == {a.symbol for a in scenarios._SELECTION_ANALYSES}


def test_neutral_reads_keep_the_analysts_own_sentence(rendered):
    """The one-sentence conclusion IS the content of a neutral read. It is
    rendered verbatim and untruncated — the change is a shape change, not a
    cap."""
    tech = _section(rendered, "Technical Analysis Reports")
    by_symbol = {a.symbol: a for a in scenarios._SELECTION_ANALYSES}
    for symbol, analysis in by_symbol.items():
        if analysis.rating != "neutral" or analysis.risk_reward is not None:
            continue
        if not analysis.reasoning:
            continue
        assert f"  Reasoning: {analysis.reasoning}" in tech


def test_a_read_with_geometry_keeps_every_field(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    sized = [
        a for a in scenarios._SELECTION_ANALYSES
        if a.rating != "neutral" and a.risk_reward is not None
    ]
    assert sized
    for analysis in sized:
        assert f"Entry: {analysis.entry_price} | Stop: {analysis.stop_loss}" in tech


def test_dissent_and_the_net_agreement_arithmetic_are_untouched(rendered):
    """Every registry symbol still gets its aligned/opposed/net line in both
    directions. This is how a seat arguing the other way reaches the PM, and
    nothing in the shape change may quieten it."""
    agreement = _section(rendered, "Independent Source Agreement")
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    registry = agent.build_evidence_registry(
        analyses=scenarios._SELECTION_ANALYSES,
        positions=scenarios._SELECTION_POSITIONS,
        news_intel=scenarios._SELECTION_NEWS,
        earnings_analyses=scenarios._SELECTION["earnings_analyses"],
        macro_analysis=scenarios._SELECTION["macro_analysis"],
        smart_money_findings=[],
        symbol_sectors={},
    )
    assert registry
    for symbol in registry:
        assert re.search(
            rf"^- {re.escape(symbol)}: \d+ aligned / \d+ opposed = net [+-]\d+ if long, "
            rf"\d+ aligned / \d+ opposed = net [+-]\d+ if short",
            agreement,
            re.M,
        ), symbol


def test_a_no_call_earnings_stance_still_reaches_the_registry(rendered):
    """A neutral earnings read is rolled up in the prose section but must
    still count as a non-aligned source in the block that ceilings size —
    otherwise shortening the input would have quietly RAISED sizing."""
    registry_section = _section(rendered, "Canonical Current Evidence Registry")
    neutral = _neutral_earnings_symbols()
    covered = {
        symbol for symbol in neutral
        if f'"{symbol}"' in registry_section
    }
    assert covered, "no rolled-up symbol survived into the registry"
    for symbol in covered:
        block = registry_section[registry_section.index(f'"{symbol}"'):][:400]
        assert '"earnings"' in block, symbol
