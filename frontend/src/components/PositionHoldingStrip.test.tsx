// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OrderItem, PositionItem } from "../api/client";
import { PositionHoldingStrip } from "./PositionHoldingStrip";

afterEach(cleanup);

function position(overrides: Partial<PositionItem> = {}): PositionItem {
  return {
    symbol: "AAPL",
    qty: 9.76,
    avg_entry: 334.78,
    current_price: 332.8,
    market_value: 3248,
    unrealized_pnl: -19.33,
    unrealized_intraday_pnl: -3.86,
    sector: "Technology",
    is_cash_equivalent: false,
    position_mark: {
      value: 332.8,
      price_kind: "broker_position_mark",
      provider: null,
      feed: null,
      market_as_of: null,
      retrieved_at: "2026-09-17T14:00:00Z",
      freshness: "unknown",
    },
    direction: "long",
    ...overrides,
  };
}

function order(overrides: Partial<OrderItem> = {}): OrderItem {
  return {
    id: "o1",
    symbol: "AAPL",
    side: "sell",
    qty: 9.76,
    order_type: "stop",
    status: "open",
    limit_price: null,
    stop_price: 315.85,
    filled_qty: null,
    filled_avg_price: null,
    submitted_at: "2026-09-17T13:00:00Z",
    filled_at: null,
    ...overrides,
  };
}

describe("PositionHoldingStrip compact row", () => {
  it("renders holding facts in one row, not a stacked details box", () => {
    render(<PositionHoldingStrip position={position()} openOrders={[order()]} trades={[]} />);

    expect(screen.getByText("AAPL")).toBeTruthy();
    expect(screen.getByText("Qty")).toBeTruthy();
    expect(screen.getByText("Entry")).toBeTruthy();
    expect(screen.getByText("Stop")).toBeTruthy();
    expect(screen.queryByText(/open position/i)).toBeNull();
    expect(screen.queryByText("Avg entry")).toBeNull();
    expect(screen.queryByText("Current price")).toBeNull();
    expect(screen.queryByText("Unrealized P&L")).toBeNull();
  });

  it("says so when no protective stop exists instead of inventing one", () => {
    render(<PositionHoldingStrip position={position()} openOrders={[]} trades={[]} />);
    expect(screen.getByText("No protective stop found")).toBeTruthy();
  });
});
