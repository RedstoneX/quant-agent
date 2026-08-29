// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TradeItem } from "../api/client";
import { TradesPanel, TradeTable } from "./TradesPanel";

// No global setup file configures auto-cleanup, so renders would otherwise
// pile up across tests in this file (see AgentPromptViewer.test.tsx).
afterEach(cleanup);

function trade(overrides: Partial<TradeItem> = {}): TradeItem {
  return {
    id: 1,
    symbol: "MRVL",
    action: "BUY",
    qty: 10,
    price: 99,
    reasoning: null,
    run_id: "run-1",
    decision_id: null,
    fill_status: "filled",
    fill_qty: 10,
    fill_price: 101.25,
    realized_pnl: null,
    timestamp: "2026-08-28 13:35:00",
    stop_loss: null,
    take_profit: null,
    ...overrides,
  };
}

// Same complaint mechanism as Orders: every click on a Trades row used to
// fire onInspect, symbol included. These pin the fix at both the
// TradesPanel level (the wired-up composite used in the app) and the
// underlying TradeTable (also rendered read-only by JournalPanel and
// CandidateDetailModal, which must keep getting plain, non-clickable
// symbol text since they pass no onSelectSymbol).
describe("TradesPanel/TradeTable symbol click vs row inspect", () => {
  it("clicking the symbol calls onSelectSymbol and not onInspect", () => {
    const onInspect = vi.fn();
    const onSelectSymbol = vi.fn();
    render(<TradesPanel trades={[trade()]} error={null} loading={false} onInspect={onInspect} onSelectSymbol={onSelectSymbol} />);

    fireEvent.click(screen.getByRole("button", { name: "MRVL" }));

    expect(onSelectSymbol).toHaveBeenCalledWith("MRVL");
    expect(onInspect).not.toHaveBeenCalled();
  });

  it("clicking a non-symbol cell still calls onInspect", () => {
    const onInspect = vi.fn();
    const onSelectSymbol = vi.fn();
    const theTrade = trade();
    render(<TradesPanel trades={[theTrade]} error={null} loading={false} onInspect={onInspect} onSelectSymbol={onSelectSymbol} />);

    fireEvent.click(screen.getByText("BUY"));

    expect(onInspect).toHaveBeenCalledWith(theTrade);
    expect(onSelectSymbol).not.toHaveBeenCalled();
  });

  it("TradeTable renders the symbol as plain text, not a dead button, when onSelectSymbol is absent (JournalPanel/CandidateDetailModal usage)", () => {
    render(<TradeTable trades={[trade()]} />);

    expect(screen.queryByRole("button", { name: "MRVL" })).toBeNull();
    expect(screen.getByText("MRVL")).toBeTruthy();
  });
});
