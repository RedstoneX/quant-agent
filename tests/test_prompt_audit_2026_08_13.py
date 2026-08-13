"""Regression guards for the safe prompt-cleanup tranche of the
2026-08-13 adversarial agent/prompt audit.

Scope note: the audit surfaced more than this branch implements. Findings
that alter behaviour or data flow — PM/RM independence (F5), the AI Risk
Manager's missing drawdown / position-age evidence (F6), routing valuation
metrics to earnings_analyst (F7b), the inherited long bias (F8), and
enforcing `premortem_check` (F4) — are deliberately NOT implemented here
and are held for external architectural review. This file guards only the
text-level corrections, all of which leave deterministic thresholds,
schemas, role boundaries and fail-closed semantics untouched.

Each test pins one finding so a future prompt edit — human, or the
meta_reflector's auto-evolve path — cannot silently reintroduce it.
"""
from pathlib import Path

import pytest

PROMPT_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

DECISION_CHAIN_PROMPTS = ("tech_analyst.md", "portfolio_manager.md", "risk_manager.md")


# ---------------------------------------------------------------------------
# F1 — R/R stated as a law of expectancy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt_name", DECISION_CHAIN_PROMPTS)
def test_rr_not_described_as_negative_expectancy(prompt_name: str) -> None:
    """"R/R < 1.5 is a negative-expectancy trade" is false as stated.

    Expectancy is `p*reward - (1-p)*risk`; the payoff ratio alone fixes
    only the BREAKEVEN hit rate (`1/(1+R/R)`), not the sign. A 1.2:1 setup
    at a 70% hit rate is strongly positive. All three decision-chain
    prompts asserted the falsehood as a hard rule, which teaches a model
    to reject profitable setups for a stated reason that is not true — and
    invites it to trust the same flawed arithmetic elsewhere.

    The thresholds themselves are unchanged and still enforced; only the
    justification was corrected.
    """
    lowered = (PROMPT_DIR / prompt_name).read_text().lower()
    assert "negative-expectancy trade" not in lowered, (
        f"{prompt_name} reintroduced the claim that a low R/R is inherently "
        f"a negative-expectancy trade. R/R sets the breakeven hit rate "
        f"(1/(1+R/R)), not the sign of expectancy. State the real objection: "
        f"the setup depends on a hit rate this system has never measured."
    )
    assert "negative expectancy." not in lowered, (
        f"{prompt_name} reintroduced 'negative expectancy' as a flat "
        f"assertion. See the docstring for why it is wrong."
    )
    assert "negative-expectancy territory" not in lowered, (
        f"{prompt_name} reintroduced 'negative-expectancy territory'."
    )


@pytest.mark.parametrize("prompt_name", DECISION_CHAIN_PROMPTS)
def test_rr_breakeven_hit_rate_is_taught(prompt_name: str) -> None:
    """The replacement must hand the model the real arithmetic, not merely
    delete the false claim — otherwise the threshold becomes an unexplained
    magic number, which for a reasoning model is worse than a wrong reason.
    """
    text = (PROMPT_DIR / prompt_name).read_text()
    assert "1/(1+" in text or "1/(1 +" in text, (
        f"{prompt_name} must teach the breakeven-hit-rate identity "
        f"`1/(1+R/R)` so the R/R thresholds are derivable rather than "
        f"asserted."
    )


@pytest.mark.parametrize("prompt_name", DECISION_CHAIN_PROMPTS)
def test_rr_operative_thresholds_unchanged(prompt_name: str) -> None:
    """The correction was to the REASON, not the rule. 1.5 must still be
    the catalyst-free floor everywhere it was before — this branch is not
    authorized to move a risk threshold.
    """
    text = (PROMPT_DIR / prompt_name).read_text()
    assert "1.5" in text, (
        f"{prompt_name} lost the 1.5 R/R floor. Correcting the "
        f"justification must not relax the threshold."
    )


# ---------------------------------------------------------------------------
# F2 — RM's R/R threshold contradicted itself
# ---------------------------------------------------------------------------

def test_rm_rr_threshold_is_internally_consistent() -> None:
    """RM's Review Checklist said "Minimum 1:2 risk-reward preferred"
    while its enforcement section, PM and Tech all key off 1.5. Two
    different numbers for the same gate in one prompt is a genuine
    contradiction, not a stylistic one — and it is exactly the drift that
    near-duplicate instructions (F10) produce over time.
    """
    text = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "1:2 risk-reward" not in text, (
        "risk_manager.md reintroduced a '1:2' R/R threshold that "
        "contradicts the 1.5 floor used by its own enforcement section, "
        "by portfolio_manager.md and by tech_analyst.md."
    )


# ---------------------------------------------------------------------------
# F3 — reasoning_chain field counts drifted from the schema
# ---------------------------------------------------------------------------

def test_pm_reasoning_chain_field_count_matches_schema() -> None:
    """PM's prompt claimed a "7-field reasoning_chain" and a "7-Step"
    framework while `ReasoningChain` carries 9 fields and the framework
    listed 8 steps. RM's prompt repeated the same wrong count when
    describing what it receives.

    A model told to produce 7 of 9 fields has been handed a reason to skip
    two — and the two it would skip are `continuity_check` and
    `premortem_check`, the only ones that validate when empty.
    """
    from src.models import ReasoningChain

    field_count = len(ReasoningChain.model_fields)
    assert field_count == 9, (
        f"ReasoningChain now has {field_count} fields; update the prompts "
        f"and this test together."
    )
    for prompt_name in ("portfolio_manager.md", "risk_manager.md"):
        text = (PROMPT_DIR / prompt_name).read_text()
        assert "7-field `reasoning_chain`" not in text, (
            f"{prompt_name} describes PM's reasoning_chain as 7-field; the "
            f"schema has {field_count}. An undercount invites skipped fields."
        )


def test_pm_names_every_reasoning_chain_field() -> None:
    """Naming all nine explicitly is what makes the count checkable, and
    what stops the next edit from re-drifting."""
    from src.models import ReasoningChain

    text = (PROMPT_DIR / "portfolio_manager.md").read_text()
    for name in ReasoningChain.model_fields:
        assert name in text, (
            f"portfolio_manager.md never names `{name}`, a field of the "
            f"schema it must populate."
        )


def test_pm_framework_step_count_is_not_understated() -> None:
    """The framework header said "7-Step" while listing eight steps; the
    eighth is the pre-mortem, i.e. the one a biased model most benefits
    from treating as optional.
    """
    text = (PROMPT_DIR / "portfolio_manager.md").read_text()
    assert "7-Step Decision Framework" not in text, (
        "portfolio_manager.md reintroduced the '7-Step' header over an "
        "8-step framework."
    )
    assert "### Step 8" in text, (
        "portfolio_manager.md lost Step 8 (the pre-mortem). It is the only "
        "step that red-teams the book against its own directional bias."
    )


# ---------------------------------------------------------------------------
# F9 — forced chain-of-thought scaffolding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prompt_name", ("portfolio_manager.md", "macro_analyst.md", "tech_analyst.md"),
)
def test_no_forced_step_by_step_scaffolding(prompt_name: str) -> None:
    """"You must think step by step / do NOT skip steps" is weak-model
    scaffolding. On a modern reasoning model it does not improve the answer
    and it pins reasoning to a fixed order, which is wrong on the days when
    a single input dominates.

    What was dropped is the instruction on HOW to think — not WHAT to
    report. The next test is the guard on that distinction.
    """
    text = (PROMPT_DIR / prompt_name).read_text().lower()
    for banned in ("think step by step", "do not skip steps", "each step builds on"):
        assert banned not in text, (
            f"{prompt_name} reintroduced forced-CoT scaffolding "
            f"({banned!r}). Specify the required coverage of "
            f"`reasoning_chain`, not the order of thought."
        )


@pytest.mark.parametrize(
    "prompt_name",
    ("portfolio_manager.md", "macro_analyst.md", "tech_analyst.md",
     "risk_manager.md", "position_reviewer.md"),
)
def test_reasoning_chain_still_mandatory(prompt_name: str) -> None:
    """The auditability requirement must survive the scaffolding removal.

    This is what keeps the previous test from being satisfiable by simply
    deleting the reasoning requirement — auditability is the property that
    makes every downstream grader (RM, evening_analyst, meta_reflector)
    possible at all.
    """
    text = (PROMPT_DIR / prompt_name).read_text()
    assert "reasoning_chain" in text and "MANDATORY" in text.upper(), (
        f"{prompt_name} must still require a populated `reasoning_chain`."
    )


# ---------------------------------------------------------------------------
# F10 — duplicated instructions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prompt_name,phrase,limit",
    (
        # RM stated "final gate before execution" in a standalone paragraph
        # AND in the Guardrails bullet, verbatim enough to read as two rules.
        ("risk_manager.md", "no further LLM review", 1),
        # position_reviewer carried the noise/signal principle verbatim in
        # both Guardrails and Money-Making Principle 1.
        ("position_reviewer.md", "with no state_change is noise", 1),
        # tech_analyst's role boundary appeared as prose AND as the
        # Guardrails Autonomy bullet.
        ("tech_analyst.md", "you do NOT size positions or place orders", 1),
    ),
)
def test_instruction_not_repeated_verbatim(
    prompt_name: str, phrase: str, limit: int,
) -> None:
    """Repetition was a weak-model technique for making an instruction
    stick. On a strong model the copies compete: near-duplicates with
    slightly different wording read as separate rules, and they drift —
    F2 is precisely that drift, caught in the act.
    """
    text = (PROMPT_DIR / prompt_name).read_text()
    count = text.count(phrase)
    assert count <= limit, (
        f"{prompt_name} repeats {phrase!r} {count} times (limit {limit}). "
        f"State it once, in the section that owns it, and cross-reference."
    )


def test_dedup_preserved_the_role_boundary_anchors() -> None:
    """Guard against over-trimming: `test_prompts_safety.py` requires each
    Guardrails section to close with a role-boundary bullet, and
    `test_prompts_contract.py` requires PM to name the four fields it must
    never emit. An earlier pass of this cleanup removed the anchors rather
    than the redundancy and broke both — pinned here so the next dedup
    trims copies, not contracts.
    """
    tech = (PROMPT_DIR / "tech_analyst.md").read_text()
    assert "**Autonomy.**" in tech, (
        "tech_analyst.md lost its Guardrails autonomy bullet."
    )
    pm = (PROMPT_DIR / "portfolio_manager.md").read_text()
    assert "**Autonomy boundary.**" in pm, (
        "portfolio_manager.md lost its Guardrails autonomy bullet."
    )
    assert "do NOT emit" in pm, (
        "portfolio_manager.md lost the explicit 'do NOT emit' statement of "
        "the PortfolioConstructor boundary."
    )
    for forbidden in ("entry_price", "stop_loss", "take_profit", "allocation_pct"):
        assert forbidden in pm, (
            f"portfolio_manager.md must still name `{forbidden}` as a field "
            f"PM does not emit."
        )
    rm = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "**Final gate.**" in rm, (
        "risk_manager.md lost its Guardrails final-gate bullet — the "
        "surviving copy after dedup."
    )


def test_rm_dedup_did_not_drop_capital_preservation() -> None:
    """The RM dedup was deliberately conservative: the "Decision rules"
    paragraph partly duplicates the Guardrails veto bullet, but it is the
    ONLY place that says "err on the side of capital preservation". A
    cleanup that silently deletes a risk-protective instruction from the
    risk agent is not a cleanup.
    """
    text = (PROMPT_DIR / "risk_manager.md").read_text().lower()
    assert "err on the side of capital preservation" in text, (
        "risk_manager.md lost its capital-preservation instruction. "
        "Deduplication must not remove the only copy of a safety rule."
    )


# ---------------------------------------------------------------------------
# F7a — earnings_analyst graded on evidence it never receives
# ---------------------------------------------------------------------------

def test_earnings_valuation_context_does_not_require_absent_market_data() -> None:
    """`EarningsAnalystAgent.build_user_message` passes ONLY the filing
    text plus symbol / form_type / filing_date. No price, no market cap,
    no multiple. Yet `valuation_context` is one of five reasoning_chain
    fields and a full column of the sentiment rubric — so a load-bearing
    axis of a PM-consumed call was resolvable only by inventing a number.

    Routing the real valuation data here (it exists — `get_valuation_metrics`
    feeds tech_analyst) is a data-flow change held for review. The
    prompt-level fix is to stop asking for a figure the agent cannot
    source, using the file's own established `[UNSOURCED:]` convention.
    """
    text = (PROMPT_DIR / "earnings_analyst.md").read_text()
    assert "[UNSOURCED:no_market_data]" in text, (
        "earnings_analyst.md must offer `[UNSOURCED:no_market_data]` for "
        "valuation_context — the agent receives no price or multiple, and "
        "its own convention is to emit a token rather than invent one."
    )
    assert "you are not given a share price" in text.lower(), (
        "earnings_analyst.md must state plainly that no price/market cap "
        "is provided. Otherwise the rubric's valuation column invites a "
        "hallucinated multiple that PM then sizes against."
    )


def test_earnings_example_does_not_quote_a_multiple() -> None:
    """The worked example is the strongest instruction in any prompt — a
    model copies its shape before it reads the rules. It must not model
    inventing a P/E.
    """
    text = (PROMPT_DIR / "earnings_analyst.md").read_text()
    assert "28x forward earnings" not in text, (
        "earnings_analyst.md's example reintroduced a fabricated multiple. "
        "The example must demonstrate a filing-grounded durability "
        "judgement instead."
    )


def test_earnings_unsourced_convention_still_intact() -> None:
    """The new token must sit alongside the three canonical reasons, not
    replace them — `test_prompts_safety.py` pins those separately, and the
    downstream consumers grep for all of them."""
    text = (PROMPT_DIR / "earnings_analyst.md").read_text()
    for reason in ("not_in_filing", "truncated", "ambiguous"):
        assert f"[UNSOURCED:{reason}]" in text, (
            f"earnings_analyst.md lost [UNSOURCED:{reason}] during the "
            f"valuation cleanup."
        )
