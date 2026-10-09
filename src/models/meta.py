from typing import Literal
from pydantic import Field, model_validator
from src.models.base import LLMOutputModel
from src.models.evening import BuyLossRootCause

# ---------------------------------------------------------------------------
# Quarterly Meta-Reflection schema (PR3+ — strategic self-audit)
# ---------------------------------------------------------------------------

# Agents that meta-reflection is ALLOWED to propose prompt edits to. The two
# excluded agents (risk_manager, position_reviewer) encode hard discipline
# (R/R ≥ 1.5, SELL triggers, cash-only); letting auto-evolution append
# "learnings" there risks diluting invariants. Explicit allow-list is safer
# than a deny-list.
MetaReflectionAgentName = Literal[
    "tech_analyst",
    "news_analyst",
    "macro_analyst",
    "earnings_analyst",
    "portfolio_manager",
    "evening_analyst",
]


class MetaReasoningChain(LLMOutputModel):
    """7-step chain the meta-reflector must fill before emitting the report.

    Parallel depth to morning PM's 7-step chain and position reviewer's
    6-step chain — empty strings fail validation so the LLM can't skip a
    step.

    **Ordering matters**: the LLM runs these in-order to avoid the
    trap of proposing prompt edits without first understanding (a) its
    own self-portrait across multiple axes, (b) where the self-portrait
    falls short of the ideal, (c) what the target agent's prompt ALREADY
    contains. Facts-first, synthesis-next, existing-design-audit,
    proposal-last.

    Design notes for anyone editing this chain:
      - Steps 1-3 are FACTS. They each cite numbers from a specific
        digest section. No interpretation allowed.
      - Step 4 is SYNTHESIS. It's the first step that interprets the
        facts, producing a multi-axis self-portrait. Replaces the old
        single-axis `style_bias_identification` + absorbs the old
        `agent_hit_rate_audit` (which was just another axis of self-
        portrait anyway).
      - Step 5 is DIAGNOSIS. It names 2-3 top leverage gaps between
        the self-portrait and the idealized trader profile the user
        wants the system to converge toward.
      - Step 6 is PROMPT AUDIT. For each gap named in step 5, the LLM
        consults `agent_prompts_snapshot` to understand what's already
        in the target agent's prompt — preventing duplicate / redundant
        / conflicting edits.
      - Step 7 is PROPOSAL. Grounded in both the gaps (step 5) AND the
        existing prompt state (step 6).

    The old `missed_theme_diagnosis` step was folded into
    `portrait_gap_diagnosis` — theme coverage IS one of the gap axes.
    """

    performance_vs_benchmark: str = Field(min_length=1)
    """Step 1/FACT. Where did this quarter's return land vs SPY? Alpha
    positive or negative? Drawdown profile? Be specific about numbers
    from period_performance — no "we did ok this quarter" hand-waving."""

    secular_theme_audit: str = Field(min_length=1)
    """Step 2/FACT. Enumerate this quarter's real themes (AI capex,
    nuclear/power, rare earth, reshoring, etc.). For each: did we
    participate? At what entry position relative to the breakout? For
    how long? Name themes_caught_early, themes_caught_late,
    themes_missed_entirely — mirror the structured output fields."""

    loss_autopsy_audit: str = Field(min_length=1)
    """Step 3/FACT. Enumerate the top 3-5 loss causes from
    loss_patterns.by_cause. For each: count, alpha_destruction_pct,
    which agent owns it. This feeds `loss_pattern_report`."""

    self_portrait_synthesis: str = Field(min_length=1)
    """Step 4/SYNTHESIS. **Multi-axis self-portrait**, not a single-
    line label. Synthesize facts from steps 1-3 + agent_signal_activity
    into concrete dimensions: (a) conviction_calibration — does HIGH
    conviction actually outperform LOW? (b) theme_breadth — do we
    cover only tech/AI or also energy/materials/reshoring? (c)
    loss_discipline — do we catch thesis breaks or ride losers? (d)
    execution_style — average hold days, realized vs intended
    timeframe. (e) agent_balance — any agent gone silent / any
    flooding with low-quality signals. Each dimension should be one
    sentence citing a specific digest number. This REPLACES the
    prior `style_bias_identification` + `agent_hit_rate_audit`."""

    portrait_gap_diagnosis: str = Field(min_length=1)
    """Step 5/DIAGNOSIS. For each dimension in the self-portrait, name
    the IDEAL state (what a medium-long-term value investor with broad
    theme coverage would look like) and the ACTUAL state. Pick the
    top 2-3 highest-leverage gaps — don't try to fix everything.
    Explicitly call out where failures happened: if a theme was
    missed, which agent layer (news vs macro vs tech vs PM) was
    responsible? Attribution is specific, not collective."""

    existing_prompt_audit: str = Field(min_length=1)
    """Step 6/PROMPT AUDIT. For each of the top gaps named in step 5,
    read `agent_prompts_snapshot[{target_agent}]` and enumerate: (a)
    what rules ALREADY exist that address this gap (cite the section /
    heading), (b) whether those existing rules are being followed
    (check corrigibility_trend — are the losses / misses recurring
    despite the rule?), (c) whether there's room for a new rule that
    doesn't conflict with or duplicate existing content. If the
    snapshot shows the target section is saturated with prior
    Learnings, propose a retract-or-replace rather than another
    append. **Do NOT propose a learning without citing what's already
    in the target prompt.**"""

    prompt_edit_reasoning: str = Field(min_length=1)
    """Step 7/PROPOSAL. Given the gaps (step 5) and existing-prompt
    state (step 6), why these specific `proposed_learnings` and not
    others? Corrigibility is the key check: if a cause has been
    worsening for 2 quarters AND the existing prompt has no rule for
    it → append. If a cause has been worsening AND an existing rule
    isn't being followed → DON'T append another (the issue is rule
    adherence, not rule absence); log this as a
    `persistent_blindspot` for the operator to review manually. If
    improving → don't pile on."""


class ThemeCoverage(LLMOutputModel):
    """Quarter-level theme participation — the core "trend capture" metric.

    All four lists may be empty. The meta-reflector populates them from its
    reading of missed_themes + holdings activity during the quarter. Not
    every theme has to appear in every bucket — a theme can be both
    "caught late" and "fully exited", those nuances are in the audit text.
    """

    themes_caught_early: list[str] = []
    """Themes we bought before the move was obvious (entry < 30% of the
    quarter's total move for that theme). The system's genuine alpha."""
    themes_caught_late: list[str] = []
    """Themes we bought after the trend was already priced (entry > 50%
    of total move). Trend-follower rather than trend-identifier
    behavior — ok occasionally, systematically problematic."""
    themes_missed_entirely: list[str] = []
    """Themes that ran ≥20% in the quarter and we never held any symbol
    within. Pure coverage / blindspot failures — the highest-value
    signal for where the system needs to look."""
    emerging_themes_to_watch: list[str] = []
    """Themes forming late in the quarter that didn't run enough to
    show in the caught/missed categories yet. Prior knowledge PM
    should carry into next quarter."""
    mispricing_patterns: list[str] = []
    """Concrete examples where earnings_analyst said bullish+high but
    PM didn't buy, or where macro_analyst tagged a sector tailwind
    and we had no coverage. 1-5 entries, each specific."""


# Mirror of src.models.BuyLossRootCause — quarterly reflector reuses the
# same taxonomy so downstream corrigibility comparisons line up.
MetaLossRootCause = BuyLossRootCause


class LossPattern(LLMOutputModel):
    """One row of loss_pattern_report.top_patterns — cause + attribution +
    proposed guard. Agent attribution drives which prompt gets the
    `proposed_guard` as a candidate learning."""

    root_cause: MetaLossRootCause
    occurrences: int = Field(ge=1)
    total_loss_pct: float
    """Signed sum of pct_move_since_buy for wrongs in this bucket — sign
    preserved so a mix of small/large isn't hidden in absolute values."""
    example_trades: list[str] = Field(min_length=1, max_length=8)
    """Concrete trades "SYMBOL YYYY-MM-DD -X%" so the prompt edit
    justification has anchors, not abstractions."""
    attributable_agent: Literal[
        "tech_analyst",
        "news_analyst",
        "macro_analyst",
        "earnings_analyst",
        "portfolio_manager",
        "evening_analyst",
        "execution",
        "no_agent",
    ]
    """`no_agent` when the failure is pure discipline (PM / evening's
    discipline — nothing any individual agent's prompt could have
    caught). `execution` when the issue was broker-side, not LLM."""
    proposed_guard: str = Field(min_length=1, max_length=400)
    """One-sentence candidate prompt addition that would have caught
    this pattern. Empty strings / vague hedges fail validation.
    400 cap is intentionally matched to MissedOpportunity.lesson — the
    LLM cites concrete facts (symbols, dates, pct moves) so terse caps
    force vague language, which is worse than the extra context."""


class LossPatternReport(LLMOutputModel):
    """Quarterly loss autopsy. Parallel structure to ThemeCoverage so the
    meta-reflector's ups/downs analysis stays symmetric."""

    top_patterns: list[LossPattern] = Field(default_factory=list, max_length=5)
    systemic_vs_alpha_split: str = Field(default="")
    """Prose one-liner decomposing losses: "72% alpha-destruction (we
    under-performed the tape), 28% systemic (market also fell)"."""
    worst_single_trade: str | None = None
    """Most painful single wrong BUY this quarter + its root cause +
    whether the pattern is likely to recur. None when no wrongs."""
    corrigibility_score: Literal["improving", "stable", "degrading"] = "stable"
    """Compared to last quarter's report — are the same causes getting
    better, holding, or worse? Drives whether to add more learnings
    (degrading) or give existing ones time to work (improving)."""


class PromptLearning(LLMOutputModel):
    """A proposed edit to one agent's prompt. Append-only for safety —
    never delete existing rules, never rewrite core sections. PR 4's
    prompt_editor enforces additional guards (length, dedup, prohibited
    words, single-quarter rate limits) on top of this schema.

    `retract` is the sole exception to append-only: used in later
    quarters to remove a learning THIS system previously added if the
    subsequent data showed it didn't help.
    """

    agent_name: MetaReflectionAgentName
    operation: Literal["append", "retract"]
    learning_text: str = Field(min_length=20, max_length=200)
    """1-2 concrete sentences. The PR 4 editor rejects entries containing
    "always"/"never"/"override"/"must always"/"must never" as these
    directly conflict with the hard-invariant wording in core prompts."""
    justification: str = Field(min_length=40)
    """Must cite specific digest facts: agent hit-rate numbers, theme
    occurrence counts, loss-cause frequencies, corrigibility deltas.
    A post-hoc model_validator enforces at least one number or '%'
    appears — no vibes-only learnings."""
    retract_target_hash: str | None = None
    """Only set when operation='retract'. Content-hash of the prior
    PromptLearning.learning_text being withdrawn. PR 4 verifies the
    hash matches an actual prior auto-append before deleting."""

    @model_validator(mode="after")
    def _justification_cites_facts(self) -> "PromptLearning":
        # Cheap heuristic — real validator (jaccard / forbidden-word check)
        # lives in the PR 4 prompt_editor. Here we just make sure the LLM
        # didn't emit a justification that's pure adjectives. At minimum
        # some numeric/percent anchor must appear.
        has_digit = any(ch.isdigit() for ch in self.justification)
        if not has_digit:
            raise ValueError(
                "PromptLearning.justification must cite at least one digest "
                "fact with a number (count, %, or quarter period). Got: "
                f"{self.justification[:80]!r}"
            )
        if self.operation == "retract" and not self.retract_target_hash:
            raise ValueError(
                "operation='retract' requires retract_target_hash pointing "
                "to the prior auto-appended learning being withdrawn"
            )
        return self


class QuarterlyMetaReflection(LLMOutputModel):
    """Top-level meta-reflector output. Persisted to
    data/evolution/{period}/reflection.json alongside the digest."""

    period: str
    """e.g. '2026-Q1' — matches the digest's period label."""
    meta_reasoning_chain: MetaReasoningChain
    style_self_portrait: str = Field(default="", max_length=2000)
    """Multi-sentence honest self-description for ongoing audit. Optional:
    `meta_reasoning_chain.self_portrait_synthesis` carries the same
    content as part of the CoT, so some LLM outputs legitimately leave
    this top-level field empty rather than duplicating. When non-empty
    it's useful for downstream continuity rendering."""
    persistent_blindspots: list[str] = Field(default_factory=list, max_length=5)
    root_cause_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    theme_coverage_report: ThemeCoverage
    loss_pattern_report: LossPatternReport
    proposed_learnings: list[PromptLearning] = Field(
        default_factory=list,
        max_length=3,
    )
    """System enforces max 3 agents edited per quarter AFTER schema
    validation — see PR 4's prompt_editor for the enforcement layer.
    This schema max is the upper bound the LLM sees."""
    confidence: Literal["high", "medium", "low"] = "medium"
    """Meta-confidence — with only 1-2 quarters of data the LLM should
    self-report 'low' and propose at most 1 learning. PR 4's editor
    uses this to scale down edit rates."""
