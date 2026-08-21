import { describe, expect, it } from "vitest";
import { LiveQuote, PriceBar } from "../api/client";
import { chartCandles } from "./PriceChartPanel";

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
});
