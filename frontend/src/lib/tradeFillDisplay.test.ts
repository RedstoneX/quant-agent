import { describe, expect, it } from "vitest";
import { displayFillPrice, displayFillQty } from "./tradeFillDisplay";

describe("trade fill display truthfulness", () => {
  it.each(["unfilled", "canceled"])("never substitutes requested values for an %s trade", (fillStatus) => {
    const trade = {
      fill_status: fillStatus,
      qty: 100,
      price: 50,
      fill_qty: null,
      fill_price: null,
    };

    expect(displayFillQty(trade)).toBe("—");
    expect(displayFillPrice(trade)).toBe("—");
  });

  it.each([
    { fillStatus: "filled", fillQty: 100, fillPrice: 50.25 },
    { fillStatus: "partially_filled", fillQty: 25, fillPrice: 50.15 },
  ])("shows canonical broker fill facts for a $fillStatus trade", ({ fillStatus, fillQty, fillPrice }) => {
    const trade = {
      fill_status: fillStatus,
      qty: 100,
      price: 50,
      fill_qty: fillQty,
      fill_price: fillPrice,
    };

    expect(displayFillQty(trade)).toBe(String(fillQty));
    expect(displayFillPrice(trade)).toBe(`$${fillPrice.toFixed(2)}`);
  });
});
