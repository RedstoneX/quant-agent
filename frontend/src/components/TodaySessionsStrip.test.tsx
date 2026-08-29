// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { TodaySessionsStrip } from "./TodaySessionsStrip";

function run(runId: string, session: string, timestamp: string): RunSummary {
  return {
    run_id: runId,
    session_prefix: session,
    first_timestamp: timestamp,
    last_timestamp: timestamp,
    agent_count: 1,
    decision_id: null,
    total_cost_usd: null,
  };
}

function funnel(runId: string): RunFunnelResponse {
  return {
    run_id: runId,
    session_prefix: "morning",
    timestamp: "2026-08-21 13:35:00",
    candidates: [],
    candidates_considered: 1,
    reached_pm_count: 1,
    proposed_order_count: 1,
    executed_count: 1,
    bearish_hedge_considered: false,
    hard_risk_block: false,
    pm_reasoning: null,
    risk_verdict: null,
    macro_context: null,
    decision_state: "executed",
  };
}

function trade(id: number, runId: string, symbol: string, fillStatus: string): TradeItem {
  return {
    id,
    symbol,
    action: "BUY",
    qty: 10,
    price: 99,
    fill_qty: 8,
    fill_price: 101.25,
    reasoning: null,
    run_id: runId,
    decision_id: null,
    fill_status: fillStatus,
    timestamp: "2026-08-21 13:36:00",
    stop_loss: null,
    take_profit: null,
  };
}

describe("TodaySessionsStrip selected-session executions", () => {
  it("shows only the selected run's fills and returns the clicked symbol trade", () => {
    // Production's legacy morning run id/prefix is `run-*`; the UI names
    // that session Morning for the operator.
    const morning = run("run-1", "run", "2026-08-21 13:35:00");
    const midday = run("midday-1", "midday", "2026-08-21 17:00:00");
    const mrvl = trade(1, morning.run_id, "MRVL", "filled");
    const later = trade(2, midday.run_id, "AAPL", "filled");
    const unfilled = { ...trade(3, morning.run_id, "NVDA", "new"), fill_qty: null, fill_price: null };
    const onSelectTrade = vi.fn();

    render(
      <TodaySessionsStrip
        runs={[morning, midday]}
        funnels={{ [morning.run_id]: funnel(morning.run_id), [midday.run_id]: funnel(midday.run_id) }}
        trades={[mrvl, later, unfilled]}
        loading={false}
        error={null}
        selectedRunId={morning.run_id}
        autoFollow
        onSelect={vi.fn()}
        onFollowLatest={vi.fn()}
        onSelectTrade={onSelectTrade}
      />
    );

    // Collapsed to a one-line summary by default (item 8) — the
    // per-session executions table is a click away, not gone.
    expect(screen.getByText(/2 sessions/)).toBeTruthy();
    expect(screen.queryByText("Morning executions")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /2 sessions/ }));

    expect(screen.getByText("Morning executions")).toBeTruthy();
    expect(screen.getByText("$101.25")).toBeTruthy();
    expect(screen.queryByText("AAPL")).toBeNull();
    expect(screen.queryByText("NVDA")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Chart MRVL BUY execution" }));
    expect(onSelectTrade).toHaveBeenCalledWith(mrvl);
  });

  it("summarizes session count, last session time, no-trade count and fill count in the collapsed line", () => {
    const morning = run("run-1", "run", "2026-08-21T13:35:00Z");
    const midday = run("midday-1", "midday", "2026-08-21T17:00:00Z");
    const morningFunnel = funnel(morning.run_id);
    const middayFunnel = { ...funnel(midday.run_id), decision_state: "no_proposal" as const };
    const mrvl = trade(1, morning.run_id, "MRVL", "filled");

    render(
      <TodaySessionsStrip
        runs={[morning, midday]}
        funnels={{ [morning.run_id]: morningFunnel, [midday.run_id]: middayFunnel }}
        trades={[mrvl]}
        loading={false}
        error={null}
        selectedRunId={morning.run_id}
        autoFollow
        onSelect={vi.fn()}
        onFollowLatest={vi.fn()}
        onSelectTrade={vi.fn()}
      />
    );

    // 2 sessions, 1 of them no-trade (midday's no_proposal), 1 real fill
    // (MRVL) recorded across the day. The clock portion is left as a
    // wildcard: it follows the runner's locale/timezone, same as every
    // other toLocaleTimeString call in this codebase (lib/format.ts's
    // fmtTime, the chart's quoteAsOf display) — pinning an exact "17:00"
    // vs "05:00 PM" string here would make this test environment-specific
    // for no reason relevant to what it verifies.
    expect(screen.getByText(/^2 sessions · last .+ · 1 no-trade · 1 fill$/)).toBeTruthy();
  });
});
