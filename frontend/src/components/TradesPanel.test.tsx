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

// Phase 6 (§6.2c): reasoning is fetched (TradeItem.reasoning) and shown
// elsewhere (CandidateDetailModal, SearchPanel) but was missing from this
// table entirely. Long strings truncate in the cell and reveal on demand,
// same disclosure idiom AgentPromptViewer.tsx uses — and, like the Symbol
// column, that disclosure must never fire the row's onInspect.
describe("TradesPanel/TradeTable reasoning column", () => {
  const LONG_REASON =
    "Thesis invalid: guidance cut on the print, high-conviction bearish read from news_analyst, trimming the full position ahead of the open.";

  it("short reasoning renders in full with no truncation control", () => {
    render(<TradeTable trades={[trade({ reasoning: "clean entry, tech breakout" })]} />);
    expect(screen.getByText("clean entry, tech breakout")).toBeTruthy();
    expect(screen.queryByText(/show more/)).toBeNull();
  });

  it("null reasoning renders a placeholder, not blank", () => {
    // stop_loss/take_profit/decision_id also render "—" for a null value on
    // this fixture, so the reasoning cell isn't the only "—" on the row —
    // assert at least one is present rather than assuming uniqueness.
    render(<TradeTable trades={[trade({ reasoning: null })]} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("long reasoning truncates and expands on click without firing onInspect", () => {
    const onInspect = vi.fn();
    const theTrade = trade({ reasoning: LONG_REASON });
    render(<TradesPanel trades={[theTrade]} error={null} loading={false} onInspect={onInspect} />);

    const toggle = screen.getByRole("button", { name: /show more/ });
    expect(toggle.textContent).not.toContain("trimming the full position");

    fireEvent.click(toggle);

    expect(onInspect).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /show less/ }).textContent).toContain(
      "trimming the full position",
    );
  });
});
