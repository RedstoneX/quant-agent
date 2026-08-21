import { describe, expect, it } from "vitest";
import { ledgerLine } from "./JournalPanel";
import { CandidateFunnelItem, RunFunnelResponse } from "../api/client";

/* 2026-08-21 Mission Control correctness tranche, section C: pins the
 * compact decision-ledger line format (candidate -> PM -> Risk -> execution
 * outcome -> reason) the Journal's top-level historical view relies on. */

function candidate(overrides: Partial<CandidateFunnelItem>): CandidateFunnelItem {
  return {
    symbol: "NVDA",
    direction: "bullish",
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
    timestamp: "2026-08-21 12:00:00",
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
    decision_state: "proposed_not_executed",
    ...overrides,
  };
}

describe("ledgerLine", () => {
  it("reads PM no proposal for a candidate that never left specialist screening", () => {
    const c = candidate({});
    expect(ledgerLine(c, funnel({}))).toBe("PM no proposal · Risk — · no order reached");
  });

  it("surfaces the specific execution_skip_reason instead of a generic not-executed line", () => {
    const c = candidate({
      reached_pm_target: true, reached_proposed_order: true, proposed_action: "BUY",
      execution_skip_reason: "stale_entry",
    });
    const f = funnel({ risk_verdict: { verdict: { approved: true, reasoning: "", reasoning_chain: {}, reason_category: "clean", modifications: [], scale_all_buys: 1 }, timestamp: null } });
    const line = ledgerLine(c, f);
    expect(line).toBe("PM BUY · Risk APPROVED · SKIPPED — stale entry · NOT EXECUTED");
  });

  it("reads EXECUTED with the real trade action when the candidate executed", () => {
    const c = candidate({ reached_pm_target: true, reached_proposed_order: true, proposed_action: "BUY", executed: true, trade_action: "BUY" });
    const f = funnel({ risk_verdict: { verdict: { approved: true, reasoning: "", reasoning_chain: {}, reason_category: "clean", modifications: [], scale_all_buys: 1 }, timestamp: null } });
    expect(ledgerLine(c, f)).toBe("PM BUY · Risk APPROVED · EXECUTED (BUY)");
  });

  it("attributes a run-wide AI Risk rejection only to candidates that reached a proposed order (run-level attribution)", () => {
    const rejected = candidate({ symbol: "TSLA", reached_pm_target: true, reached_proposed_order: true, proposed_action: "BUY" });
    const notProposed = candidate({ symbol: "AAPL", reached_pm_target: true, reached_proposed_order: false });
    const f = funnel({
      risk_verdict: { verdict: { approved: false, reasoning: "R/R failed", reasoning_chain: {}, reason_category: "rr_fail", modifications: [], scale_all_buys: 1 }, timestamp: null },
    });
    expect(ledgerLine(rejected, f)).toBe("PM BUY · Risk REJECTED · NOT EXECUTED");
    expect(ledgerLine(notProposed, f)).toBe("PM target, no order · Risk — · no order reached");
  });

  it("labels a hard-risk-block run's proposed candidates as blocked by the deterministic gate, not AI Risk", () => {
    const c = candidate({ reached_pm_target: true, reached_proposed_order: true, proposed_action: "SELL" });
    const f = funnel({ hard_risk_block: true });
    expect(ledgerLine(c, f)).toBe("PM SELL · Risk BLOCKED (hard gate) · NOT EXECUTED");
  });

  it("shows a per-candidate risk modification even on an otherwise-approved run", () => {
    const c = candidate({ reached_pm_target: true, reached_proposed_order: true, proposed_action: "BUY", risk_modified: true, executed: true, trade_action: "BUY" });
    const f = funnel({ risk_verdict: { verdict: { approved: true, reasoning: "", reasoning_chain: {}, reason_category: "oversized", modifications: [], scale_all_buys: 1 }, timestamp: null } });
    expect(ledgerLine(c, f)).toBe("PM BUY · Risk MODIFIED · EXECUTED (BUY)");
  });
});
