import { describe, expect, it } from "vitest";
import { displayFillPrice, displayFillQty } from "./tradeFillDisplay";

describe("trade fill display truthfulness", () => {
  it("never substitutes requested values for an unfilled or cancelled trade", () => {
    const cancelledTrade = {
      fill_status: "canceled",
      qty: 100,
      price: 50,
      fill_qty: null,
      fill_price: null,
    };

    expect(displayFillQty(cancelledTrade)).toBe("—");
    expect(displayFillPrice(cancelledTrade)).toBe("—");
  });

  it("shows canonical broker fill facts when they are known", () => {
    const partiallyFilledTrade = {
      fill_status: "partially_filled",
      qty: 100,
      price: 50,
      fill_qty: 25,
      fill_price: 50.15,
    };

    expect(displayFillQty(partiallyFilledTrade)).toBe("25");
    expect(displayFillPrice(partiallyFilledTrade)).toBe("$50.15");
  });
});
