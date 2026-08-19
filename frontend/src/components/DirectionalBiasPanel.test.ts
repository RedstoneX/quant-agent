import { describe, expect, it } from "vitest";
import { CandidateFunnelItem, RunFunnelResponse } from "../api/client";
import { computeAggregates, exposureDirection } from "./DirectionalBiasPanel";

/* Regression coverage for the instrument-signal-vs-market-exposure fix:
 * the Directional Bias panel used to bucket CandidateFunnelItem.direction
 * (the instrument's own tech_analyst-derived signal) directly as bullish/
 * bearish market exposure. For an approved inverse ETF (is_bearish_hedge),
 * a bullish instrument signal (BUY SQQQ) expresses BEARISH exposure to
 * the underlying index — the opposite of what a naive read of `direction`
 * would suggest. These tests pin exposureDirection()'s flip and
 * computeAggregates()'s two separate counters (instrument vs exposure)
 * so this can never silently regress back into mislabeling inverse-ETF
 * bullishness as bullish market exposure. */

function candidate(overrides: Partial<CandidateFunnelItem>): CandidateFunnelItem {
  return {
    symbol: "TEST",
    direction: "neutral",
    is_bearish_hedge: false,
    reached_pm_target: false,
    pm_target_weight_pct: null,
    reached_proposed_order: false,
    proposed_action: null,
    risk_modified: false,
    executed: false,
    trade_action: null,
    ...overrides,
  };
}

function funnel(overrides: Partial<RunFunnelResponse>): RunFunnelResponse {
  return {
    run_id: "run-test",
    session_prefix: "run",
    timestamp: "2026-08-19 12:00:00",
    candidates: [],
    candidates_considered: 0,
    reached_pm_count: 0,
    proposed_order_count: 0,
    executed_count: 0,
    bearish_hedge_considered: false,
    hard_risk_block: false,
    pm_reasoning: null,
    risk_verdict: null,
    macro_context: null,
    decision_state: "no_candidates",
    ...overrides,
  };
}

describe("exposureDirection", () => {
  it("passes ordinary long-instrument direction through unchanged", () => {
    expect(exposureDirection("bullish", false)).toBe("bullish");
    expect(exposureDirection("bearish", false)).toBe("bearish");
    expect(exposureDirection("neutral", false)).toBe("neutral");
    expect(exposureDirection("unknown", false)).toBe("unknown");
  });

  it("flips an inverse-ETF's instrument direction to its effective exposure", () => {
    // AAPL bullish instrument signal => bullish market exposure (baseline, non-hedge).
    expect(exposureDirection("bullish", false)).toBe("bullish");
    // SQQQ bullish-on-instrument => bearish market exposure (the actual bug being fixed).
    expect(exposureDirection("bullish", true)).toBe("bearish");
    // A bearish instrument signal on an inverse ETF must not be mislabeled
    // as bearish market exposure — it flips to bullish exposure.
    expect(exposureDirection("bearish", true)).toBe("bullish");
  });

  it("leaves neutral/unknown exposure unflipped even on a hedge instrument", () => {
    expect(exposureDirection("neutral", true)).toBe("neutral");
    expect(exposureDirection("unknown", true)).toBe("unknown");
  });
});

describe("computeAggregates — candidate direction vs exposure", () => {
  it("AAPL bullish (non-hedge) counts as bullish in both instrument and exposure buckets", () => {
    const f = funnel({
      candidates: [candidate({ symbol: "AAPL", direction: "bullish", is_bearish_hedge: false })],
      candidates_considered: 1,
    });
    const agg = computeAggregates([f], 0);
    expect(agg.candidateDirectionCounts).toEqual({ bullish: 1, bearish: 0, neutralOrUnknown: 0 });
    expect(agg.candidateExposureCounts).toEqual({ bullish: 1, bearish: 0, neutralOrUnknown: 0 });
  });

  it("SQQQ bullish-on-instrument counts as bullish instrument but bearish exposure", () => {
    const f = funnel({
      candidates: [candidate({ symbol: "SQQQ", direction: "bullish", is_bearish_hedge: true })],
      candidates_considered: 1,
      bearish_hedge_considered: true,
    });
    const agg = computeAggregates([f], 0);
    expect(agg.candidateDirectionCounts).toEqual({ bullish: 1, bearish: 0, neutralOrUnknown: 0 });
    expect(agg.candidateExposureCounts).toEqual({ bullish: 0, bearish: 1, neutralOrUnknown: 0 });
  });

  it("mixed candidate set: instrument and exposure totals genuinely diverge, not just membership", () => {
    // 3 candidates all read "bullish" at the instrument level (AAPL, MSFT,
    // and SQQQ) — a naive reading would call this "3 bullish, 0 bearish".
    // But SQQQ is an inverse ETF, so the true exposure picture is 2
    // bullish / 1 bearish. This is exactly the case the fix exists for:
    // the headline count itself must change, not just which symbol lands
    // in which bucket.
    const f = funnel({
      candidates: [
        candidate({ symbol: "AAPL", direction: "bullish", is_bearish_hedge: false }),
        candidate({ symbol: "MSFT", direction: "bullish", is_bearish_hedge: false }),
        candidate({ symbol: "SQQQ", direction: "bullish", is_bearish_hedge: true }),
      ],
      candidates_considered: 3,
      bearish_hedge_considered: true,
    });
    const agg = computeAggregates([f], 0);
    expect(agg.candidateDirectionCounts).toEqual({ bullish: 3, bearish: 0, neutralOrUnknown: 0 });
    expect(agg.candidateExposureCounts).toEqual({ bullish: 2, bearish: 1, neutralOrUnknown: 0 });
    expect(agg.totalCandidates).toBe(3);
  });

  it("applies the same instrument-vs-exposure split to PM proposals, not just raw candidates", () => {
    const f = funnel({
      candidates: [
        candidate({
          symbol: "SQQQ",
          direction: "bullish",
          is_bearish_hedge: true,
          reached_proposed_order: true,
          proposed_action: "BUY",
        }),
        candidate({
          symbol: "AAPL",
          direction: "bullish",
          is_bearish_hedge: false,
          reached_proposed_order: false,
        }),
      ],
      candidates_considered: 2,
      bearish_hedge_considered: true,
    });
    const agg = computeAggregates([f], 0);
    // Only SQQQ reached a proposed order.
    expect(agg.proposedTotal).toBe(1);
    expect(agg.proposedDirectionCounts).toEqual({ bullish: 1, bearish: 0, neutralOrUnknown: 0 });
    expect(agg.proposedExposureCounts).toEqual({ bullish: 0, bearish: 1, neutralOrUnknown: 0 });
  });

  it("a bearish instrument signal on an inverse ETF is not mislabeled as bearish exposure", () => {
    const f = funnel({
      candidates: [candidate({ symbol: "SH", direction: "bearish", is_bearish_hedge: true })],
      candidates_considered: 1,
      bearish_hedge_considered: true,
    });
    const agg = computeAggregates([f], 0);
    expect(agg.candidateDirectionCounts).toEqual({ bullish: 0, bearish: 1, neutralOrUnknown: 0 });
    expect(agg.candidateExposureCounts).toEqual({ bullish: 1, bearish: 0, neutralOrUnknown: 0 });
  });
});
