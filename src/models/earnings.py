from datetime import date
from typing import Literal
from pydantic import Field, field_validator, model_validator
from src.models.base import LLMOutputModel, _normalize_enum_case_fields, _normalize_symbol
from src.models.analysis import (
    AnalystVerdict,
    NO_STATED_STRENGTH,
    Nomination,
    VerdictEvidence,
    _sanitize_nominations_field,
)


class EarningsSegment(LLMOutputModel):
    name: str
    revenue: str
    growth: str = "not disclosed"


class EarningsRevenue(LLMOutputModel):
    total: str
    yoy_growth: str = "not disclosed"
    segments: list[EarningsSegment] = []


class EarningsProfitability(LLMOutputModel):
    gross_margin: str = "not disclosed"
    operating_margin: str = "not disclosed"
    net_income: str = "not disclosed"
    eps: str = "not disclosed"


class EarningsCashFlow(LLMOutputModel):
    operating_cf: str = "not disclosed"
    free_cf: str = "not disclosed"
    capex: str = "not disclosed"


class EarningsBalanceSheet(LLMOutputModel):
    cash_and_equivalents: str = "not disclosed"
    total_debt: str = "not disclosed"
    assessment: str = "not disclosed"


class EarningsStrategicDirection(LLMOutputModel):
    key_initiatives: list[str] = []
    capital_allocation: str = "not disclosed"
    competitive_positioning: str = "not disclosed"


class EarningsRiskFlags(LLMOutputModel):
    strategic_risks: list[str] = []
    operational_risks: list[str] = []


class EarningsReasoningChain(LLMOutputModel):
    """5-step CoT for fundamental analysis — why sentiment is what it is.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.
    """

    fundamental_quality: str = Field(min_length=1)  # revenue, margin, cash flow trajectory
    growth_trajectory: str = Field(min_length=1)  # YoY / QoQ direction, momentum, inflection
    strategic_risks: str = Field(min_length=1)  # biggest strategic bets and their execution risk
    management_execution: str = Field(min_length=1)  # is management doing what they said? any pivots?
    # NOT "is the market pricing this fairly" — the agent is given filing text
    # and nothing else (no share price, no market cap, no multiple), so it
    # cannot answer that and inventing an answer is what it used to do. Reads
    # "how conditional is the story": what must keep holding for the disclosed
    # trajectory to justify any premium. See config/prompts/earnings_analyst.md.
    valuation_context: str = Field(min_length=1)


class EarningsInvestmentImplications(LLMOutputModel):
    sentiment: Literal["bullish", "bearish", "neutral"]
    conviction: Literal["high", "medium", "low"]
    reasoning_chain: EarningsReasoningChain
    key_thesis: str
    bull_case: str = "not disclosed"
    bear_case: str = "not disclosed"

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("sentiment", "conviction"),
        )


class EarningsAnalysis(LLMOutputModel):
    symbol: str
    form_type: Literal["10-Q", "10-K"]
    filing_date: str
    revenue: EarningsRevenue
    profitability: EarningsProfitability
    cash_flow: EarningsCashFlow
    balance_sheet: EarningsBalanceSheet
    management_highlights: list[str] = []
    guidance: str
    strategic_direction: EarningsStrategicDirection = EarningsStrategicDirection()
    risk_flags: EarningsRiskFlags | list[str] = EarningsRiskFlags()
    strategy_consistency: str = "No prior filing available for comparison"
    investment_implications: EarningsInvestmentImplications
    data_quality: str
    # Phase 9 (§9.1): a filing that materially changes the picture — most
    # often for the symbol this very filing is about, since a blowout beat
    # on a name Technical never rated is exactly the gap Phase 9 closes.
    # Default [] so an analysis saved to disk before this field existed
    # still loads unchanged (EarningsAnalystAgent._load_analysis).
    #
    # This is the per-FILING output model, not a per-run container — no
    # per-run earnings container exists in this codebase (`earnings_results`
    # on RunContext is a plain `list[dict]` the pipeline assembles, not a
    # Pydantic model). A session that analyzes multiple new filings makes
    # one LLM call per filing, so nominations are aggregated across every
    # filing's analysis this run (`_collect_seat_nominations`), the same
    # way `earnings_results` itself already aggregates per-filing output.
    nominations: list[Nomination] = []

    def to_verdict(self) -> "AnalystVerdict":
        """This filing's read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion: every field is read off
        `investment_implications`, which the earnings analyst already fills
        and already validates (`EarningsInvestmentImplications`) — no new
        prompting.

        direction  — `investment_implications.sentiment`, verbatim (already
                     bullish/bearish/neutral, the exact vocabulary the shared
                     shape uses).
        conviction — `investment_implications.conviction`, verbatim.
        magnitude  — UNLIKE Technical (which has two directional rungs a
                     side — buy/strong_buy — to encode as 0.5/1.0), earnings
                     sentiment is a single bullish/bearish rung with no
                     numeric or ordinal strength field anywhere on this
                     model or `EarningsInvestmentImplications`: `risk_flags`
                     is unstructured lists of free-text risks, and
                     `data_quality` is free prose, not a graded scale.
                     Inventing a gradient from either would be a fake
                     precision this seat cannot back. So every call carries
                     `NO_STATED_STRENGTH` (None) — no distance claimed, rather
                     than a distance borrowed off Technical's scale. The seat
                     still reaches the ranking through its weighted
                     conviction.

                     REVIEWED 2026-09-13 (retired item 31) and KEPT
                     unchanged — this was the only one of the four new seats
                     that did not invent a gradient, and the other three were
                     brought to this shape rather than the reverse. The bare
                     0.5 became the shared constant so there is one number.
        evidence   — `key_thesis` (the seat's own summary of its call) plus
                     the five reasoning-chain steps, labelled, plus
                     `data_quality` when the analyst said anything past the
                     bare default.
        invalidation — the case the analyst built AGAINST its own call:
                     `bear_case` for a bullish read, `bull_case` for a
                     bearish one. Both fields default to the literal string
                     "not disclosed" when the analyst didn't fill them in
                     (see the field definitions above) — that placeholder is
                     not a real falsifier, so it is treated as blank rather
                     than passed through as if it were content. UNLIKE
                     Technical, there is no numeric stop-price to fall back
                     to here, so a directional call whose falsifier is left
                     undisclosed ends up with a blank `invalidation` and
                     `AnalystVerdict`'s own validator refuses to construct
                     it — this seat cannot manufacture a falsifier out of
                     nothing, and an error at construction is more honest
                     than inventing one.
        """
        impl = self.investment_implications
        direction = impl.sentiment
        # Absent either way — None, not 0.0. See `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH

        evidence: list[VerdictEvidence] = []
        if impl.key_thesis.strip():
            evidence.append(VerdictEvidence(label="key_thesis", text=impl.key_thesis.strip()))
        chain = impl.reasoning_chain
        for label in (
            "fundamental_quality",
            "growth_trajectory",
            "strategic_risks",
            "management_execution",
            "valuation_context",
        ):
            text = getattr(chain, label, "") or ""
            if text.strip():
                evidence.append(VerdictEvidence(label=label, text=text.strip()))
        data_quality = (self.data_quality or "").strip()
        if data_quality and data_quality.lower() != "not disclosed":
            evidence.append(VerdictEvidence(label="data_quality", text=data_quality))

        if direction == "bullish":
            falsifier = impl.bear_case
        elif direction == "bearish":
            falsifier = impl.bull_case
        else:
            falsifier = ""
        falsifier = (falsifier or "").strip()
        invalidation = "" if falsifier.lower() == "not disclosed" else falsifier

        return AnalystVerdict(
            seat="earnings",
            symbol=self.symbol,
            direction=direction,
            magnitude=magnitude,
            conviction=impl.conviction,
            evidence=evidence,
            invalidation=invalidation,
        )

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("filing_date")
    @classmethod
    def validate_filing_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("guidance", "data_quality")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)
