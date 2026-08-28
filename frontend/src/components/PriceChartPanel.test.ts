import { describe, expect, it } from "vitest";
import { LiveQuote, PositionItem, PriceBar } from "../api/client";
import { chartCandles, entryPriceLine, shouldShowPrevClose, tradeMarkers } from "./PriceChartPanel";

function position(overrides: Partial<PositionItem> = {}): PositionItem {
  return {
    symbol: "MRVL",
    qty: 10,
    avg_entry: 240,
    current_price: 250,
    market_value: 2500,
    unrealized_pnl: 100,
    unrealized_intraday_pnl: null,
    sector: null,
    is_cash_equivalent: false,
    direction: "long",
    ...overrides,
  };
}

const COLORS = { green: "green", red: "red" };

const history: PriceBar[] = [
  { date: "2026-08-20", open: 248, high: 253, low: 247, close: 251.01, volume: 1_000 },
];

describe("chartCandles", () => {
  it("appends today's forming candle at live-last rather than leaving history looking current", () => {
    const quote: LiveQuote = {
      symbol: "MRVL",
      last_price: 237.07,
      prev_close: 251.01,
      session_open: 246,
      session_high: 247,
      session_low: 235,
    };

    expect(chartCandles(history, quote, "2026-08-21")).toEqual([
      { time: "2026-08-20", open: 248, high: 253, low: 247, close: 251.01 },
      { time: "2026-08-21", open: 246, high: 247, low: 235, close: 237.07 },
    ]);
  });

  it("does not fabricate a candle from a partial quote", () => {
    const quote: LiveQuote = {
      symbol: "MRVL",
      last_price: 237.07,
      prev_close: 251.01,
      session_open: null,
      session_high: null,
      session_low: null,
    };

    expect(chartCandles(history, quote, "2026-08-21")).toHaveLength(1);
  });

  it("uses timestamped intraday bars without adding a synthetic daily candle", () => {
    const intraday: PriceBar[] = [{
      date: "2026-08-21",
      timestamp: "2026-08-21T13:30:00+00:00",
      open: 252.22, high: 252.26, low: 248, close: 249.18, volume: 1000,
    }];
    const quote: LiveQuote = {
      symbol: "MRVL", last_price: 237.07, prev_close: 251.01,
      session_open: 252.22, session_high: 252.26, session_low: 233.33,
    };
    const candles = chartCandles(intraday, quote, "2026-08-21", "5m");
    expect(candles).toHaveLength(1);
    expect(candles[0].time).toBe(Math.floor(new Date(intraday[0].timestamp!).getTime() / 1000));
    expect(candles[0].close).toBe(249.18);
  });
});

describe("tradeMarkers", () => {
  it("places an execution on its containing five-minute candle", () => {
    const barTime = Math.floor(new Date("2026-08-21T13:30:00+00:00").getTime() / 1000);
    const markers = tradeMarkers(
      "MRVL",
      [{
        id: 1, symbol: "MRVL", action: "BUY", qty: 2, price: 249.84,
        fill_qty: 2, fill_price: 249.18, reasoning: null, run_id: "run-1",
        decision_id: null, fill_status: "filled",
        timestamp: "2026-08-21 13:34:46", stop_loss: null, take_profit: null,
      }],
      { green: "green", red: "red" },
      "5m",
      [barTime as never]
    );
    expect(markers).toHaveLength(1);
    expect(markers[0].time).toBe(barTime);
    expect(markers[0].text).toBe("BUY 2");
  });
});

describe("entryPriceLine", () => {
  it("draws the held symbol's average entry, green when the position is up", () => {
    const line = entryPriceLine("MRVL", [position({ avg_entry: 240, unrealized_pnl: 100 })], COLORS);
    expect(line).toEqual({ price: 240, color: "green", title: "ENTRY 10 @ $240.00 · +$100.00" });
  });

  it("colors the line red when the position is down", () => {
    const line = entryPriceLine("MRVL", [position({ unrealized_pnl: -50 })], COLORS);
    expect(line?.color).toBe("red");
  });

  it("returns null when no symbol is charted", () => {
    expect(entryPriceLine(null, [position()], COLORS)).toBeNull();
  });

  it("returns null when the symbol is not held", () => {
    expect(entryPriceLine("AAPL", [position({ symbol: "MRVL" })], COLORS)).toBeNull();
  });

  it("returns null rather than a fabricated line when avg_entry is missing or zero", () => {
    expect(entryPriceLine("MRVL", [position({ avg_entry: 0 })], COLORS)).toBeNull();
    expect(entryPriceLine("MRVL", [position({ avg_entry: null as unknown as number })], COLORS)).toBeNull();
  });
});

describe("shouldShowPrevClose", () => {
  it("is dropped on the 1D view", () => {
    expect(shouldShowPrevClose("1d")).toBe(false);
  });

  it("still shows on intraday views", () => {
    expect(shouldShowPrevClose("5m")).toBe(true);
    expect(shouldShowPrevClose("15m")).toBe(true);
    expect(shouldShowPrevClose("1h")).toBe(true);
  });
});
