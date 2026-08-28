import { describe, expect, it } from "vitest";
import { OrderItem, PositionItem, TradeItem } from "../api/client";
import { distanceToStop, findPositionStop, stopBandRange } from "./positionStop";

function position(overrides: Partial<PositionItem> = {}): PositionItem {
  return {
    symbol: "DIS",
    qty: 3,
    avg_entry: 90,
    current_price: 95,
    market_value: 285,
    unrealized_pnl: 15,
    unrealized_intraday_pnl: null,
    sector: null,
    is_cash_equivalent: false,
    direction: "long",
    ...overrides,
  };
}

function order(overrides: Partial<OrderItem> = {}): OrderItem {
  return {
    id: "o1",
    symbol: "DIS",
    side: "sell",
    qty: 3,
    order_type: "stop",
    status: "open",
    limit_price: null,
    stop_price: 85,
    filled_qty: null,
    filled_avg_price: null,
    submitted_at: "2026-08-27T13:00:00Z",
    filled_at: null,
    ...overrides,
  };
}

function trade(overrides: Partial<TradeItem> = {}): TradeItem {
  return {
    id: 1,
    symbol: "DIS",
    action: "BUY",
    qty: 3,
    price: 90,
    reasoning: null,
    run_id: "run-1",
    decision_id: null,
    fill_status: "filled",
    fill_qty: 3,
    fill_price: 90,
    timestamp: "2026-08-25 13:30:00",
    stop_loss: 82,
    take_profit: null,
    ...overrides,
  };
}

describe("findPositionStop", () => {
  it("prefers a resting stop order at the broker over a trade record", () => {
    const stop = findPositionStop(position(), [order({ stop_price: 85 })], [trade({ stop_loss: 82 })]);
    expect(stop).toEqual({ price: 85, source: "open_order", detail: "Resting stop order at the broker" });
  });

  it("falls back to the most recent entry trade's recorded stop when no resting order exists", () => {
    const older = trade({ id: 1, timestamp: "2026-08-20 13:30:00", stop_loss: 80 });
    const newer = trade({ id: 2, timestamp: "2026-08-25 13:30:00", stop_loss: 82 });
    const stop = findPositionStop(position(), [], [older, newer]);
    expect(stop?.price).toBe(82);
    expect(stop?.source).toBe("trade_record");
  });

  it("ignores an open order for a different symbol or the wrong side", () => {
    const wrongSymbol = order({ symbol: "AAPL" });
    const wrongSide = order({ side: "buy" });
    const nonStopType = order({ order_type: "limit" });
    const stop = findPositionStop(position(), [wrongSymbol, wrongSide, nonStopType], [trade({ stop_loss: 82 })]);
    expect(stop?.source).toBe("trade_record");
  });

  it("uses the closing side matching a short/bearish qty sign", () => {
    const short = position({ qty: -3, symbol: "SQQQ", direction: "bearish_hedge" });
    const buyStop = order({ symbol: "SQQQ", side: "buy", stop_price: 12 });
    expect(findPositionStop(short, [buyStop], [])?.price).toBe(12);
    expect(findPositionStop(short, [order({ symbol: "SQQQ", side: "sell", stop_price: 12 })], [])).toBeNull();
  });

  it("returns null when neither source has a usable stop", () => {
    expect(findPositionStop(position(), [], [])).toBeNull();
    expect(findPositionStop(position(), [], [trade({ stop_loss: null })])).toBeNull();
  });

  it("never fabricates a stop from a HOLD or unfilled trade", () => {
    const unfilled = trade({ fill_status: "new", fill_qty: null, stop_loss: 82 });
    expect(findPositionStop(position(), [], [unfilled])).toBeNull();
  });
});

describe("stopBandRange", () => {
  it("orders low/high regardless of whether the stop is above or below current price", () => {
    expect(stopBandRange(95, 85)).toEqual({ low: 85, high: 95 });
    expect(stopBandRange(10, 12)).toEqual({ low: 10, high: 12 });
  });
});

describe("distanceToStop", () => {
  it("computes amount and percent distance from current price to the stop", () => {
    const result = distanceToStop(95, 85);
    expect(result.amount).toBe(10);
    expect(result.pct).toBeCloseTo(10.526, 2);
  });

  it("returns a null percent rather than dividing by zero", () => {
    expect(distanceToStop(0, 85).pct).toBeNull();
  });
});
