import { describe, expect, it } from "vitest";
import { OrderItem, TradeItem } from "../api/client";
import { recordedOrderTarget } from "./orderTarget";

function order(overrides: Partial<OrderItem> = {}): OrderItem {
  return {
    id: "stop-1",
    symbol: "AAPL",
    side: "sell",
    qty: 12,
    order_type: "stop",
    status: "accepted",
    limit_price: null,
    stop_price: 218.4,
    filled_qty: 0,
    filled_avg_price: null,
    submitted_at: "2026-08-25T14:21:00Z",
    filled_at: null,
    ...overrides,
  };
}

function trade(overrides: Partial<TradeItem> = {}): TradeItem {
  return {
    id: 11,
    symbol: "AAPL",
    action: "BUY",
    qty: 12,
    price: 221.4,
    reasoning: null,
    run_id: "run-1",
    decision_id: "d1",
    broker_order_id: "alpaca-1",
    fill_status: "filled",
    fill_qty: 12,
    fill_price: 221.45,
    timestamp: "2026-08-25T14:18:00Z",
    stop_loss: 218.4,
    take_profit: 232,
    ...overrides,
  };
}

describe("recordedOrderTarget", () => {
  it("uses the trade that carries this order's broker id", () => {
    expect(recordedOrderTarget(order({ id: "alpaca-1" }), [trade()])).toBe(232);
  });

  it("uses the symbol's one recorded take-profit for a later protective stop", () => {
    expect(recordedOrderTarget(order(), [trade()])).toBe(232);
  });

  it("returns null when two different take-profits are on the book for that name", () => {
    expect(
      recordedOrderTarget(order(), [
        trade({ id: 1, take_profit: 232 }),
        trade({ id: 2, broker_order_id: "other", take_profit: 240 }),
      ]),
    ).toBeNull();
  });

  it("returns null when no take-profit was recorded", () => {
    expect(recordedOrderTarget(order(), [trade({ take_profit: null })])).toBeNull();
  });
});
