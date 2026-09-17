// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PositionItem } from "../api/client";
import { HoldingsStrip } from "./HoldingsStrip";

afterEach(() => {
  cleanup();
});

function position(overrides: Partial<PositionItem> = {}): PositionItem {
  return {
    symbol: "AAPL",
    qty: 10,
    avg_entry: 100,
    current_price: 110,
    market_value: 1100,
    unrealized_pnl: 100,
    unrealized_intraday_pnl: null,
    sector: null,
    is_cash_equivalent: false,
    position_mark: {
      value: null,
      price_kind: "broker_position_mark",
      provider: null,
      feed: null,
      market_as_of: null,
      retrieved_at: "2026-01-01T00:00:00Z",
      freshness: "unknown",
    },
    direction: "long",
    ...overrides,
  };
}

describe("HoldingsStrip", () => {
  it("wraps at four cards per row and does not use a horizontal slider", () => {
    const positions = ["AAPL", "RSG", "NOK", "AMD", "MRVL", "ETN", "EQNR", "SGOV"].map((symbol, index) =>
      position({
        symbol,
        market_value: 1000 - index,
        is_cash_equivalent: symbol === "SGOV",
        direction: symbol === "SGOV" ? "cash_equivalent" : "long",
      }),
    );
    render(<HoldingsStrip positions={positions} error={null} updatedAt={new Date("2026-08-25T18:30:00Z")} />);
    const grid = screen.getByLabelText("Holdings").querySelector(".grid");
    expect(grid?.className).toContain("xl:grid-cols-4");
    expect(grid?.className).not.toContain("overflow-x-auto");
    expect(screen.getByLabelText("Holdings").className).toContain("overflow-x-hidden");
  });

  it("collapses to a summary line in compact chrome until expanded", () => {
    const positions = ["AAPL", "RSG", "NOK", "AMD", "MRVL"].map((symbol, index) =>
      position({ symbol, market_value: 1000 - index }),
    );
    render(
      <HoldingsStrip
        positions={positions}
        error={null}
        updatedAt={new Date("2026-08-25T18:30:00Z")}
        compact
      />,
    );
    expect(screen.getByLabelText("Holdings").querySelector(".grid")).toBeNull();
    expect(screen.getByText("5 open")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Holdings/ }));
    expect(screen.getByLabelText("Holdings").querySelector(".grid")?.className).toContain("xl:grid-cols-4");
  });

  it("always shows the wrap grid inside a Dockview panel", () => {
    const positions = ["AAPL", "RSG", "NOK", "AMD", "MRVL"].map((symbol, index) =>
      position({ symbol, market_value: 1000 - index }),
    );
    render(
      <HoldingsStrip
        positions={positions}
        error={null}
        updatedAt={new Date("2026-08-25T18:30:00Z")}
        compact
        variant="panel"
      />,
    );
    expect(screen.getByLabelText("Holdings").querySelector(".grid")?.className).toContain("xl:grid-cols-4");
    expect(screen.queryByRole("button", { name: /Holdings/ })).toBeNull();
  });
});
