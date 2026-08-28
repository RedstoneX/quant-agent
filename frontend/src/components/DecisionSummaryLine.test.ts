import { describe, expect, it } from "vitest";
import { CandidateDetailResponse, CandidateFunnelItem, RunFunnelResponse } from "../api/client";
import { summarizeDecision } from "./DecisionSummaryLine";

function detail(overrides: Partial<CandidateDetailResponse>): CandidateDetailResponse {
  return {
    run_id: "run-test",
    symbol: "DIS",
    decision_id: null,
    tech: null,
    earnings: null,
    news_symbol: [],
    macro_context: null,
    news_context: null,
    pm_reasoning: null,
    pm_target: null,
    pm_proposed_order: null,
    risk_verdict: null,
    risk_modification: null,
    trade: null,
    consensus: { signals: [], agreement: "insufficient_data" },
    ...overrides,
  };
}

function candidate(overrides: Partial<CandidateFunnelItem>): CandidateFunnelItem {
  return {
    symbol: "DIS",
    direction: "bullish",
    is_bearish_hedge: false,
    reached_pm_target: true,
    pm_target_weight_pct: 5,
    pm_risk_allocation_pct: null,
    reached_proposed_order: true,
    proposed_action: "BUY",
    risk_modified: false,
    executed: false,
    trade_action: null,
    execution_skip_reason: null,
    execution_skip_detail: null,
    ...overrides,
  };
}

function funnel(candidates: CandidateFunnelItem[], overrides: Partial<RunFunnelResponse> = {}): RunFunnelResponse {
  return {
    run_id: "run-test",
    session_prefix: "run",
    timestamp: "2026-08-21 12:00:00",
    candidates,
    candidates_considered: candidates.length,
    reached_pm_count: candidates.length,
    proposed_order_count: candidates.length,
    executed_count: 0,
    bearish_hedge_considered: false,
    hard_risk_block: false,
    pm_reasoning: null,
    risk_verdict: null,
    macro_context: null,
    decision_state: "proposed_not_executed",
    ...overrides,
  };
}

describe("summarizeDecision", () => {
  it("returns null when nothing has reached any stage — never a placeholder line", () => {
    const d = detail({});
    const f = funnel([candidate({ reached_pm_target: false, reached_proposed_order: false })]);
    expect(summarizeDecision(d, f)).toBeNull();
  });

  it("summarizes a proposed-and-modified-and-allowed candidate in one line", () => {
    const d = detail({
      pm_proposed_order: { action: "BUY", symbol: "DIS", allocation_pct: 20, entry_price: 100, stop_loss: 90, take_profit: 120, reasoning: "x" },
      risk_verdict: {
        timestamp: "2026-08-21 12:00:30",
        verdict: {
          approved: true,
          reason_category: "oversized",
          reasoning: "trimmed",
          modifications: [],
          reasoning_chain: {},
          scale_all_buys: 1,
        },
      },
      risk_modification: { symbol: "DIS", field: "allocation_pct", original_value: 20, new_value: 12, reason: "sizing" },
    });
    const f = funnel([candidate({})]);
    expect(summarizeDecision(d, f)).toBe("PM proposed BUY 20%, Risk cut allocation pct to 12, gate allowed");
  });

  it("summarizes a rejected candidate without claiming a gate/execution outcome it never reached", () => {
    const d = detail({
      pm_proposed_order: { action: "SELL", symbol: "DIS", allocation_pct: 8, entry_price: 50, stop_loss: 55, take_profit: 40, reasoning: "x" },
      risk_verdict: {
        timestamp: "2026-08-21 12:00:30",
        verdict: {
          approved: false,
          reason_category: "rr_fail",
          reasoning: "bad r/r",
          modifications: [],
          reasoning_chain: {},
          scale_all_buys: 1,
        },
      },
    });
    const f = funnel([candidate({})]);
    expect(summarizeDecision(d, f)).toBe("PM proposed SELL 8%, Risk rejected");
  });

  it("includes execution when a trade was recorded", () => {
    const d = detail({
      pm_proposed_order: { action: "BUY", symbol: "DIS", allocation_pct: 5, entry_price: 10, stop_loss: 8, take_profit: 15, reasoning: "x" },
      trade: {
        id: 1, symbol: "DIS", action: "BUY", qty: 3, price: 10, reasoning: null, run_id: "run-test",
        decision_id: null, fill_status: "filled", fill_qty: 3, fill_price: 10, timestamp: "2026-08-21 12:01:00",
        stop_loss: null, take_profit: null,
      },
    });
    const f = funnel([candidate({ executed: true })]);
    expect(summarizeDecision(d, f)).toBe("PM proposed BUY 5%, gate allowed, executed BUY");
  });
});
