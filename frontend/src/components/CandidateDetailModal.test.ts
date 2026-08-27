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
    pm_target_weight_pct: 5, pm_risk_allocation_pct: null,
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
    expect(exec.caption).not.toMatch(/execution reason was not recorded/i);
    expect(gate.status).toBe("blocked");
    expect(gate.caption).toContain("stale entry");
  });

  it("states plainly that the execution reason was not recorded when a proposed order went unexecuted with no skip reason (external review finding, 2026-08-21)", () => {
    // Regression test #2 from external review: proposed order exists, not
    // executed, execution_skip_reason absent. Must not invent a reason —
    // and must not read as if "not executed this run" were itself an
    // explanation.
    const c = candidate({ execution_skip_reason: null, execution_skip_detail: null });
    const d = detail({ pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 5, entry_price: 100, stop_loss: 90, take_profit: 120, reasoning: "r" } });
    const stages = buildCandidateStages(d, funnel([c]));
    const exec = stages.find((s) => s.key === "exec")!;
    expect(exec.caption).toMatch(/execution reason was not recorded/i);
  });

  it("keeps the exact persisted skip reason as authoritative over the generic unknown-execution-reason text (external review finding, 2026-08-21, regression test #3)", () => {
    const withReason = candidate({ execution_skip_reason: "insufficient_cash", execution_skip_detail: "needed $637.80, had $145.11" });
    const d = detail({ pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 10, entry_price: 63.78, stop_loss: 59, take_profit: 73.34, reasoning: "r" } });
    const stages = buildCandidateStages(d, funnel([withReason]));
    const exec = stages.find((s) => s.key === "exec")!;
    expect(exec.caption).toContain("insufficient cash");
    expect(exec.caption).toContain("$145.11");
    expect(exec.caption).not.toMatch(/execution reason was not recorded/i);
  });

  it("states plainly that no candidate-specific reason was recorded for a target with no order — never restates the target itself as the reason (external review finding, 2026-08-21, regression test #1)", () => {
    // "Target 5%" is evidence (what PM set), not a reason (why nothing
    // followed) — this pins the exact wrong-before/right-after behavior
    // external review flagged.
    const c = candidate({ reached_proposed_order: false, proposed_action: null });
    const d = detail({ pm_target: { symbol: "NVDA", target_weight_pct: 5, conviction: "high", thesis: "t", thesis_invalid_if: "x" } });
    const stages = buildCandidateStages(d, funnel([c]));
    const pm = stages.find((s) => s.key === "pm")!;
    expect(pm.status).toBe("reached");
    expect(pm.caption).not.toBe("Target 5%");
    expect(pm.caption).toMatch(/set a target of 5%/i);
    expect(pm.caption).toMatch(/no order was constructed/i);
    expect(pm.caption).toMatch(/candidate-specific reason for not constructing an order was not recorded/i);

    // This candidate's journey necessarily ends at PM (nothing downstream
    // is reachable without a proposed order), so the OutcomeBanner's
    // "Reason" text is this same explicit, truthful caption.
    const furthest = furthestReachedStage(stages);
    expect(furthest?.key).toBe("pm");
    expect(furthest?.caption).toBe(pm.caption);
  });

  it("shows the constructed order's action, not the target weight, once an order actually exists", () => {
    const c = candidate({ proposed_action: "BUY" });
    const d = detail({
      pm_target: { symbol: "NVDA", target_weight_pct: 9, conviction: "high", thesis: "t", thesis_invalid_if: "x" },
      pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 9, entry_price: 121.4, stop_loss: 112, take_profit: 138, reasoning: "r" },
    });
    const stages = buildCandidateStages(d, funnel([c]));
    const pm = stages.find((s) => s.key === "pm")!;
    expect(pm.caption).toBe("BUY");
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

  it("attributes a hard-risk-block run's gate caption to the deterministic gate, not AI Risk — for a candidate that reached a proposed order", () => {
    // PM can construct a proposed order before the deterministic gate
    // blocks the whole run (the gate runs after PM, before risk_manager —
    // see the risk_gate forensic-row docs in src/api/schemas.py). Without
    // pm_proposed_order set, this candidate would never have reached a
    // proposed order at all, in which case the hard block must NOT be
    // attributed to it either — see the next describe block.
    const c = candidate({});
    const d = detail({
      pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 5, entry_price: 100, stop_loss: 90, take_profit: 120, reasoning: "r" },
    });
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

  it("attributes an AI-Risk rejection to the risk stage, distinct from an execution skip — for a candidate that reached a proposed order", () => {
    const c = candidate({});
    const d = detail({
      pm_proposed_order: { action: "BUY", symbol: "NVDA", allocation_pct: 8, entry_price: 248, stop_loss: 232, take_profit: 280, reasoning: "r" },
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

describe("buildCandidateStages — candidate-specific risk/PM attribution (external review finding, 2026-08-21)", () => {
  // risk_verdict (and hard_risk_block) are RUN-scoped: the AI Risk Manager
  // evaluates one run's whole batch of proposed orders together. A
  // candidate that never itself reached a PM proposed order was never
  // evaluated by Risk, no matter what the run's verdict says about a
  // DIFFERENT candidate's proposed order. These pin the exact regression
  // scenario external review specified: specialist evidence present, no
  // PM target, no PM proposed order, a run-wide verdict exists for other
  // candidates — this candidate must not show a Risk outcome or "stopped
  // at AI Risk" for either an approved or a rejected run-wide verdict.
  function specialistOnlyDetail(verdictApproved: boolean): CandidateDetailResponse {
    return detail({
      tech: {
        symbol: "NVDA", rating: "neutral", conviction: "low",
        entry_price: null, reference_target: null, stop_loss: null,
        reasoning_chain: {}, reasoning: "Chopping in a range, no clean signal.",
        thesis_invalid_if: "n/a", signal_age_days: 3,
      },
      pm_target: null,
      pm_proposed_order: null,
      risk_verdict: {
        verdict: {
          approved: verdictApproved,
          reasoning: verdictApproved ? "Other proposed orders this run were clean approvals." : "Other proposed orders this run failed the R/R audit.",
          reasoning_chain: {}, reason_category: verdictApproved ? "clean" : "rr_fail",
          modifications: [], scale_all_buys: 1,
        },
        timestamp: "2026-08-21 12:00:00",
      },
    });
  }

  function specialistOnlyFunnelCandidate(): CandidateFunnelItem {
    return candidate({ reached_pm_target: false, reached_proposed_order: false, proposed_action: null });
  }

  it("does not attribute a run-wide APPROVED verdict to a candidate that never reached a proposed order", () => {
    const d = specialistOnlyDetail(true);
    const f = funnel([specialistOnlyFunnelCandidate()], {
      risk_verdict: { verdict: { approved: true, reasoning: "x", reasoning_chain: {}, reason_category: "clean", modifications: [], scale_all_buys: 1 }, timestamp: null },
    });
    const stages = buildCandidateStages(d, f);

    const pm = stages.find((s) => s.key === "pm")!;
    const risk = stages.find((s) => s.key === "risk")!;
    expect(pm.status).toBe("not_reached");
    expect(risk.status).toBe("not_reached");
    expect(risk.caption).toBeUndefined();

    const furthest = furthestReachedStage(stages);
    expect(furthest?.key).toBe("specialists");
    expect(furthest?.label).not.toMatch(/risk/i);
    expect(furthest?.caption).toContain("did not select this candidate");
    expect(furthest?.caption).toContain("candidate-specific reason was not recorded");
  });

  it("does not attribute a run-wide REJECTED verdict to a candidate that never reached a proposed order", () => {
    const d = specialistOnlyDetail(false);
    const f = funnel([specialistOnlyFunnelCandidate()], {
      risk_verdict: { verdict: { approved: false, reasoning: "x", reasoning_chain: {}, reason_category: "rr_fail", modifications: [], scale_all_buys: 1 }, timestamp: null },
    });
    const stages = buildCandidateStages(d, f);

    const risk = stages.find((s) => s.key === "risk")!;
    const gate = stages.find((s) => s.key === "gate")!;
    const exec = stages.find((s) => s.key === "exec")!;
    expect(risk.status).toBe("not_reached");
    expect(risk.caption).toBeUndefined();
    // The gate/exec captions must not borrow the run's rejection either —
    // this candidate never reached the gate or execution at all, so there
    // is nothing candidate-specific to report at either stage.
    expect(gate.caption).toBeUndefined();
    expect(exec.caption).toBeUndefined();

    const furthest = furthestReachedStage(stages);
    expect(furthest?.key).toBe("specialists");
    expect(furthest?.label).not.toMatch(/risk/i);
  });

  it("still attributes hard_risk_block to a candidate ONLY when it reached a proposed order (mirrors CandidateRail.candidateStage)", () => {
    const d = specialistOnlyDetail(true); // risk_verdict present is irrelevant once hard-blocked
    const f = funnel([specialistOnlyFunnelCandidate()], { hard_risk_block: true });
    const stages = buildCandidateStages(d, f);
    const risk = stages.find((s) => s.key === "risk")!;
    const gate = stages.find((s) => s.key === "gate")!;
    expect(risk.status).toBe("not_reached");
    expect(gate.status).toBe("not_reached");
    expect(gate.caption).toBeUndefined();
  });
});
