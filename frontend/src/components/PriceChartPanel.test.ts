import { describe, expect, it } from "vitest";
import { LiveQuote, PriceBar } from "../api/client";
import { chartCandles, tradeMarkers } from "./PriceChartPanel";

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
