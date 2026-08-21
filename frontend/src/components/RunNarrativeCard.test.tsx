// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { render, fireEvent, within } from "@testing-library/react";
import { RunNarrativeCard } from "./JournalPanel";
import { ModalProvider } from "../context/ModalContext";
import { CandidateFunnelItem, RunFunnelResponse, RunSummary } from "../api/client";

/* External review finding (2026-08-22): the Journal's per-run candidate
 * chip previously called onOpenCandidate(dayRuns, symbol) and let App.tsx
 * search the WHOLE day for the first run containing that symbol — wrong
 * whenever the same symbol appears in more than one run that day, since it
 * always opened the first match regardless of which run card was actually
 * clicked. RunNarrativeCard now calls openCandidateDetail(run.run_id,
 * symbol) directly, using the run_id it already has in scope. This proves
 * clicking NVDA inside the 13:00 run's card opens the 13:00 run's
 * candidate detail, not the morning run's, when NVDA appears in both. */

function runSummary(runId: string, prefix: string): RunSummary {
  return {
    run_id: runId, session_prefix: prefix,
    first_timestamp: "2026-08-21 13:00:00", last_timestamp: "2026-08-21 13:05:00",
    agent_count: 3, decision_id: null, total_cost_usd: 0.01,
  };
}

function nvdaCandidate(overrides: Partial<CandidateFunnelItem> = {}): CandidateFunnelItem {
  return {
    symbol: "NVDA", direction: "bullish", is_bearish_hedge: false,
    reached_pm_target: true, pm_target_weight_pct: 5,
    reached_proposed_order: true, proposed_action: "BUY",
    risk_modified: false, executed: false, trade_action: null,
    execution_skip_reason: null, execution_skip_detail: null,
    ...overrides,
  };
}

function funnelWithNvda(runId: string): RunFunnelResponse {
  return {
    run_id: runId, session_prefix: "run", timestamp: "2026-08-21 13:00:00",
    candidates: [nvdaCandidate()],
    candidates_considered: 1, reached_pm_count: 1, proposed_order_count: 1, executed_count: 0,
    bearish_hedge_considered: false, hard_risk_block: false,
    pm_reasoning: null, risk_verdict: null, macro_context: null,
    decision_state: "proposed_not_executed",
  };
}

describe("RunNarrativeCard — opens the exact run clicked, not a day-wide search match", () => {
  it("clicking NVDA in the 13:00 run's card opens the 13:00 run_id, not the morning run's", () => {
    const morningRun = runSummary("run-morning01", "run");
    const middayRun = runSummary("midday-abcd1234", "midday");

    const openCandidateDetail = vi.fn();
    const providerValue = { openRunDetail: vi.fn(), openCandidateDetail, closeModal: vi.fn() };

    // Two independent renders — mirrors JournalPanel mapping one
    // RunNarrativeCard per run, each genuinely isolated (no shared DOM),
    // so there is no ambiguity about which card's button gets clicked.
    const morning = render(
      <ModalProvider value={providerValue}>
        <RunNarrativeCard run={morningRun} funnel={funnelWithNvda(morningRun.run_id)} funnelLoading={false} dayTrades={[]} onOpenRun={vi.fn()} />
      </ModalProvider>
    );
    const midday = render(
      <ModalProvider value={providerValue}>
        <RunNarrativeCard run={middayRun} funnel={funnelWithNvda(middayRun.run_id)} funnelLoading={false} dayTrades={[]} onOpenRun={vi.fn()} />
      </ModalProvider>
    );

    // render() binds its top-level queries to document.body, not to the
    // subtree it just mounted — with both cards mounted at once (by
    // design, to prove clicking one never affects the other), scope each
    // query to its own container via within() rather than each result's
    // own top-level getByRole, which would otherwise see BOTH NVDA buttons.
    fireEvent.click(within(midday.container).getByRole("button", { name: /NVDA/i }));

    expect(openCandidateDetail).toHaveBeenCalledTimes(1);
    expect(openCandidateDetail).toHaveBeenCalledWith("midday-abcd1234", "NVDA");

    // And the morning card's own NVDA button, if clicked, opens the
    // morning run — proving this isn't coincidentally always returning
    // whichever run_id happens to be "first".
    fireEvent.click(within(morning.container).getByRole("button", { name: /NVDA/i }));
    expect(openCandidateDetail).toHaveBeenCalledTimes(2);
    expect(openCandidateDetail).toHaveBeenLastCalledWith("run-morning01", "NVDA");

    morning.unmount();
    midday.unmount();
  });
});
