import { describe, expect, it } from "vitest";
import { CandidateFunnelItem, RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { bestPrimaryRunId, candidateStage, isSweepOnlyExecution } from "./funnelShared";

/* Regression coverage for two truth fixes found by inspecting this branch's
 * frontend against real (read-only, loopback) production data on a live
 * trading day:
 *
 * 1. bestPrimaryRunId — QAMC's afternoon position-review sessions (midday/
 *    close) are structurally distinct from a full opportunity scan and
 *    near-always report zero candidates. Picking the literal-latest run
 *    unconditionally meant a routine close-of-day review silently erased a
 *    real morning scan's candidates/decisions from the cockpit for the
 *    rest of the day. This pins the fix against the exact shape of real
 *    same-day data observed: close (0 candidates, latest) after two real
 *    "run" sessions (12+ candidates each, earlier).
 * 2. isSweepOnlyExecution — a run whose only trade is SGOV cash-sweep
 *    housekeeping still reports decision_state "executed" from the
 *    backend (any trade row counts), which reads exactly like a real
 *    strategy entry unless the UI distinguishes them. */

function run(overrides: Partial<RunSummary>): RunSummary {
  return {
    run_id: "run-test",
    session_prefix: "run",
    first_timestamp: "2026-08-20 13:30:00",
    last_timestamp: "2026-08-20 13:30:00",
    agent_count: 1,
    decision_id: null,
    total_cost_usd: null,
    ...overrides,
  };
}

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
    execution_skip_reason: null,
    execution_skip_detail: null,
    ...overrides,
  };
}

function funnel(overrides: Partial<RunFunnelResponse>): RunFunnelResponse {
  return {
    run_id: "run-test",
    session_prefix: "run",
    timestamp: "2026-08-20 13:30:00",
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

function trade(overrides: Partial<TradeItem>): TradeItem {
  return {
    id: 1,
    symbol: "SGOV",
    action: "SWEEP_BUY",
    qty: 1,
    price: 100,
    reasoning: null,
    run_id: "run-test",
    decision_id: null,
    fill_status: "filled",
    timestamp: null,
    stop_loss: null,
    take_profit: null,
    ...overrides,
  };
}

describe("bestPrimaryRunId", () => {
  it("returns null when there are no runs today", () => {
    expect(bestPrimaryRunId([], {})).toBeNull();
  });

  it("falls back to the literal latest run when every run today has zero candidates", () => {
    const runs = [
      run({ run_id: "close-1", session_prefix: "close", first_timestamp: "2026-08-20 19:30:00" }),
      run({ run_id: "midday-1", session_prefix: "midday", first_timestamp: "2026-08-20 17:00:00" }),
    ];
    const funnels = {
      "close-1": funnel({ run_id: "close-1", candidates_considered: 0 }),
      "midday-1": funnel({ run_id: "midday-1", candidates_considered: 0 }),
    };
    expect(bestPrimaryRunId(runs, funnels)).toBe("close-1");
  });

  it("prefers the latest run that actually had candidates over a later empty position-review run — the real bug reproduced from production", () => {
    const runs = [
      run({ run_id: "close-3b6b7606", session_prefix: "close", first_timestamp: "2026-08-20 19:30:52" }),
      run({ run_id: "midday-592cf267", session_prefix: "midday", first_timestamp: "2026-08-20 17:00:54" }),
      run({ run_id: "run-5593a8c9", session_prefix: "run", first_timestamp: "2026-08-20 14:00:52" }),
      run({ run_id: "run-cbf2adbd", session_prefix: "run", first_timestamp: "2026-08-20 13:30:53" }),
    ];
    const funnels = {
      "close-3b6b7606": funnel({ run_id: "close-3b6b7606", candidates_considered: 0, decision_state: "no_candidates" }),
      "midday-592cf267": funnel({ run_id: "midday-592cf267", candidates_considered: 0, decision_state: "no_candidates" }),
      "run-5593a8c9": funnel({ run_id: "run-5593a8c9", candidates_considered: 76, decision_state: "no_proposal" }),
      "run-cbf2adbd": funnel({ run_id: "run-cbf2adbd", candidates_considered: 74, decision_state: "no_proposal" }),
    };
    // Must pick the LATEST run with real candidates (14:00), not the
    // earliest (13:30) and not the literal-latest empty one (19:30).
    expect(bestPrimaryRunId(runs, funnels)).toBe("run-5593a8c9");
  });

  it("treats a not-yet-fetched funnel (undefined) as zero candidates rather than throwing", () => {
    const runs = [run({ run_id: "run-1", first_timestamp: "2026-08-20 14:00:00" })];
    expect(bestPrimaryRunId(runs, {})).toBe("run-1");
  });
});

describe("isSweepOnlyExecution", () => {
  it("is false when there are no trades", () => {
    expect(isSweepOnlyExecution([])).toBe(false);
  });

  it("is true when every trade this run is cash-sweep housekeeping", () => {
    expect(isSweepOnlyExecution([trade({ action: "SWEEP_BUY" })])).toBe(true);
    expect(isSweepOnlyExecution([trade({ action: "SWEEP_BUY" }), trade({ action: "SWEEP_SELL", id: 2 })])).toBe(true);
  });

  it("is false when a real strategy trade is mixed in with sweep trades", () => {
    expect(
      isSweepOnlyExecution([trade({ action: "SWEEP_BUY" }), trade({ id: 2, symbol: "AAPL", action: "BUY" })])
    ).toBe(false);
  });

  it("is false for an ordinary strategy trade", () => {
    expect(isSweepOnlyExecution([trade({ symbol: "AAPL", action: "BUY" })])).toBe(false);
  });
});

describe("candidateStage label truthfulness", () => {
  it("buckets a candidate that never reached PM as 'rejected' regardless of its specialist-read direction", () => {
    // The bucket itself is still named "rejected" internally (STAGE_META's
    // display label is what changed, from "Rejected by specialist" to "No
    // PM target" — this test just pins that a bearish-direction non-
    // advancing candidate isn't somehow classified differently than a
    // bullish one; the data available never distinguishes "specialist said
    // no" from "PM passed for portfolio-balance/other reasons").
    const f = funnel({ risk_verdict: null, hard_risk_block: false });
    expect(candidateStage(candidate({ direction: "bearish" }), f)).toBe("rejected");
    expect(candidateStage(candidate({ direction: "bullish" }), f)).toBe("rejected");
  });
});
