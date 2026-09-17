// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChartSymbolBar } from "./ChartSymbolBar";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mockCompany(body: { symbol: string; name: string | null }) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ...body, error: null }),
    }),
  );
}

describe("ChartSymbolBar", () => {
  it("shows the cached company name with the ticker and Lifecycle", async () => {
    mockCompany({ symbol: "AAPL", name: "Apple Inc." });
    render(
      <ChartSymbolBar
        symbol="AAPL"
        canOpenLifecycle
        onOpenLifecycle={() => undefined}
      />,
    );

    await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeTruthy());
    expect(screen.getByText("AAPL")).toBeTruthy();
    expect(screen.getByRole("button", { name: /lifecycle/i })).toBeTruthy();
  });

  it("shows the ticker only when the cache has no name — never a guessed title", async () => {
    mockCompany({ symbol: "ZZZZ", name: null });
    render(
      <ChartSymbolBar
        symbol="ZZZZ"
        canOpenLifecycle={false}
        onOpenLifecycle={() => undefined}
      />,
    );

    await waitFor(() => expect(screen.getByText("ZZZZ")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /lifecycle/i })).toBeNull();
    expect(screen.queryByText("null")).toBeNull();
  });

  it("does not mix the default market-context symbol with a real name", async () => {
    mockCompany({ symbol: "AAPL", name: "Apple Inc." });
    render(
      <ChartSymbolBar
        symbol="AAPL"
        previousSymbol="SPY"
        onGoBack={() => undefined}
        canOpenLifecycle
        onOpenLifecycle={() => undefined}
      />,
    );

    await waitFor(() => expect(screen.getByText("Apple Inc.")).toBeTruthy());
    expect(screen.getByText("AAPL")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /back to spy/i })).toBeNull();
    expect(screen.queryByText(/←/)).toBeNull();
  });
});
