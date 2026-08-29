// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OrderItem } from "../api/client";
import { OrdersPanel } from "./OrdersPanel";

// No global setup file configures auto-cleanup, so renders would otherwise
// pile up across tests in this file (see AgentPromptViewer.test.tsx).
afterEach(cleanup);

function order(overrides: Partial<OrderItem> = {}): OrderItem {
  return {
    id: "order-1",
    symbol: "MRVL",
    side: "buy",
    qty: 10,
    order_type: "market",
    status: "filled",
    limit_price: null,
    stop_price: null,
    filled_qty: 10,
    filled_avg_price: 101.25,
    submitted_at: "2026-08-28 13:35:00",
    filled_at: "2026-08-28 13:35:02",
    ...overrides,
  };
}

// Direct complaint: the owner clicks a symbol in Orders expecting the
// PositionsPanel behavior (chart the symbol, no popup) but instead gets
// a Run Detail modal, because every click anywhere on the row — the
// symbol included — used to fire onInspect. These tests pin the fix:
// the symbol cell is its own click target that calls onSelectSymbol and
// stops the click from also reaching onInspect, while every other cell
// in the row still reaches onInspect exactly as before.
describe("OrdersPanel symbol click vs row inspect", () => {
  it("clicking the symbol calls onSelectSymbol and not onInspect", () => {
    const onInspect = vi.fn();
    const onSelectSymbol = vi.fn();
    render(
      <OrdersPanel
        orders={[order()]}
        error={null}
        loading={false}
        status="all"
        onStatusChange={vi.fn()}
        onInspect={onInspect}
        onSelectSymbol={onSelectSymbol}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "MRVL" }));

    expect(onSelectSymbol).toHaveBeenCalledWith("MRVL");
    expect(onInspect).not.toHaveBeenCalled();
  });

  it("clicking a non-symbol cell still calls onInspect", () => {
    const onInspect = vi.fn();
    const onSelectSymbol = vi.fn();
    const theOrder = order();
    render(
      <OrdersPanel
        orders={[theOrder]}
        error={null}
        loading={false}
        status="all"
        onStatusChange={vi.fn()}
        onInspect={onInspect}
        onSelectSymbol={onSelectSymbol}
      />
    );

    fireEvent.click(screen.getByText("BUY"));

    expect(onInspect).toHaveBeenCalledWith(theOrder);
    expect(onSelectSymbol).not.toHaveBeenCalled();
  });

  it("renders the symbol as plain text, not a dead button, when onSelectSymbol is absent", () => {
    render(
      <OrdersPanel
        orders={[order()]}
        error={null}
        loading={false}
        status="all"
        onStatusChange={vi.fn()}
      />
    );

    expect(screen.queryByRole("button", { name: "MRVL" })).toBeNull();
    expect(screen.getByText("MRVL")).toBeTruthy();
  });
});
