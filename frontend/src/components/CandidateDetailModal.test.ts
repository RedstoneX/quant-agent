import { describe, expect, it } from "vitest";
import { buildCandidateStages, furthestReachedStage, skipText } from "./CandidateDetailModal";
import { CandidateDetailResponse, CandidateFunnelItem, RunFunnelResponse } from "../api/client";

/* 2026-08-21 Mission Control correctness tranche, sections D/E: these pin
 * the "why wasn't it purchased" derivation so a specific persisted
 * execution_skip_reason can never again silently fall back to a generic
 * "proposed but not executed" phrase, and so an unrecorded reason is always
 * stated as such rather than left implicit. */

function detail(overrides: Partial<CandidateDetailResponse>): CandidateDetailResponse {
  return {
    run_id: "run-test",
    symbol: "NVDA",
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
    symbol: "NVDA",
    direction: "bullish",
    is_bearish_hedge: false,
    reached_pm_target: true,
    pm_target_weight_pct: 5,
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

describe("skipText", () => {
  it("returns null when no reason was persisted", () => {
    expect(skipText(null, null)).toBeNull();
  });

  it("humanizes an underscored reason with no detail", () => {
    expect(skipText("stale_entry", null)).toBe("stale entry");
  });

  it("appends detail when present", () => {
    expect(skipText("insufficient_cash", "needed $145.11, had $12.03")).toBe(
      "insufficient cash — needed $145.11, had $12.03"
    );
  });
});

describe("furthestReachedStage", () => {
  it("returns null when every stage is not_reached", () => {
    const stages = [
      { key: "a", label: "A", status: "not_reached" as const },
      { key: "b", label: "B", status: "not_reached" as const },
    ];
    expect(furthestReachedStage(stages)).toBeNull();
  });

  it("returns the LAST reached stage, not the first", () => {
    const stages = [
      { key: "a", label: "A", status: "reached" as const },
      { key: "b", label: "B", status: "reached" as const },
      { key: "c", label: "C", status: "not_reached" as const },
    ];
    expect(furthestReachedStage(stages)?.key).toBe("b");
  });
});

describe("buildCandidateStages — execution_skip_reason surfacing (section D/E)", () => {
  it("surfaces a persisted skip reason instead of a generic 'not executed' caption", () => {
    const c = candidate({ execution_skip_reason: "stale_entry", execution_skip_detail: "price moved 2.1% outside entry tolerance" });
    const d = detail({ pm_target: { symbol: "NVDA", target_weight_pct: 5, conviction: "high", thesis: "t", thesis_invalid_if: "x" } });
    const stages = buildCandidateStages(d, funnel([c]));
    const exec = stages.find((s) => s.key === "exec")!;
    const gate = stages.find((s) => s.key === "gate")!;
    expect(exec.status).toBe("blocked");
    expect(exec.caption).toContain("stale entry");
    expect(exec.caption).toContain("price moved 2.1%");
    expect(exec.caption).not.toContain("candidate-specific reason was not recorded");
    expect(gate.status).toBe("blocked");
    expect(gate.caption).toContain("stale entry");
  });

  it("states plainly that no reason was recorded when none was persisted", () => {
    const c = candidate({ execution_skip_reason: null, execution_skip_detail: null });
    const d = detail({ pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 5, entry_price: 100, stop_loss: 90, take_profit: 120, reasoning: "r" } });
    const stages = buildCandidateStages(d, funnel([c]));
    const exec = stages.find((s) => s.key === "exec")!;
    expect(exec.caption).toContain("candidate-specific reason was not recorded");
  });

  it("marks the execution stage EXECUTED when the funnel says so, with action/qty", () => {
    const c = candidate({ executed: true, trade_action: "BUY" });
    const d = detail({ trade: { id: 1, symbol: "NVDA", action: "BUY", qty: 10, price: 150, reasoning: null, run_id: "run-test", decision_id: null, fill_status: "filled", timestamp: "2026-08-21 12:05:00", stop_loss: null, take_profit: null } });
    const stages = buildCandidateStages(d, funnel([c], { executed_count: 1 }));
    const exec = stages.find((s) => s.key === "exec")!;
    expect(exec.status).toBe("executed");
    expect(exec.caption).toContain("BUY");
    expect(exec.caption).toContain("10");
  });

  it("attributes a hard-risk-block run's gate caption to the deterministic gate, not AI Risk", () => {
    const c = candidate({});
    const d = detail({});
    const stages = buildCandidateStages(d, funnel([c], { hard_risk_block: true }));
    const gate = stages.find((s) => s.key === "gate")!;
    expect(gate.status).toBe("blocked");
    expect(gate.caption).toMatch(/hard-risk gate/i);
  });

  it("marks a SELL/exit candidate EXECUTED — SELL, not the BUY-only 'purchased' framing", () => {
    // A funnel candidate isn't always a new BUY — position-exit SELLs can
    // appear here too. Pins the direction-aware fix so a future edit can't
    // silently regress back to a hardcoded "PURCHASED" that would misstate
    // a SELL/exit outcome.
    const c = candidate({ executed: true, trade_action: "SELL", proposed_action: "SELL" });
    const d = detail({
      trade: { id: 2, symbol: "NVDA", action: "SELL", qty: 15, price: 198.05, reasoning: null, run_id: "run-test", decision_id: null, fill_status: "filled", timestamp: "2026-08-21 14:41:00", stop_loss: null, take_profit: null },
      pm_proposed_order: { action: "SELL", symbol: "NVDA", allocation_pct: 3, entry_price: 198.2, stop_loss: 0, take_profit: 0, reasoning: "trim" },
    });
    const stages = buildCandidateStages(d, funnel([c], { executed_count: 1 }));
    const exec = stages.find((s) => s.key === "exec")!;
    expect(exec.status).toBe("executed");
    expect(exec.caption).toContain("SELL");
  });

  it("attributes an AI-Risk rejection to the risk stage, distinct from an execution skip", () => {
    const c = candidate({});
    const d = detail({
      risk_verdict: {
        verdict: {
          approved: false, reasoning: "R/R failed", reasoning_chain: {}, reason_category: "rr_fail",
          modifications: [], scale_all_buys: 1,
        },
        timestamp: "2026-08-21 12:00:00",
      },
    });
    const stages = buildCandidateStages(d, funnel([c]));
    const risk = stages.find((s) => s.key === "risk")!;
    const exec = stages.find((s) => s.key === "exec")!;
    expect(risk.status).toBe("rejected");
    expect(exec.caption).toMatch(/rejected by the ai risk manager/i);
  });
});
