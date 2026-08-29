// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SearchPanel } from "./SearchPanel";
import { api } from "../api/client";
import { ModalProvider } from "../context/ModalContext";

// No global setup file configures auto-cleanup, so renders would otherwise
// pile up across tests in this file (see AgentPromptViewer.test.tsx).
afterEach(cleanup);

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, search: vi.fn() } };
});

const search = api.search as unknown as ReturnType<typeof vi.fn>;

function renderPanel(onSelectSymbol = vi.fn()) {
  const openRunDetail = vi.fn();
  const openCandidateDetail = vi.fn();
  render(
    <ModalProvider value={{ openRunDetail, openCandidateDetail, closeModal: vi.fn() }}>
      <SearchPanel onSelectSymbol={onSelectSymbol} />
    </ModalProvider>
  );
  return { openRunDetail, openCandidateDetail };
}

async function runSearch(term: string) {
  fireEvent.change(screen.getByPlaceholderText(/search trade reasoning/i), { target: { value: term } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(search).toHaveBeenCalled());
}

// Direct complaint: every result row in both hit tables used to fire
// openRunDetail on ANY cell click, including cells with no visual hint
// they were part of a clickable row — and the trade-hit table has a
// symbol column that should chart the symbol (governing principle), not
// open a pop-up. These tests pin the fix per the owner's decision: the
// agent-call-hit table (no symbol column — a run IS the only meaningful
// target) keeps its row click; the trade-hit table's symbol cell charts
// instead, while the rest of that row still opens Run Detail.
describe("SearchPanel result-table clicks", () => {
  it("agent-call-hit row click still opens Run Detail", async () => {
    search.mockResolvedValue({
      query: "pm",
      trades: [],
      agent_logs: [
        { kind: "agent_log", id: 1, agent_name: "portfolio_manager", run_id: "run-9", timestamp: "2026-08-27 13:00:00", model: "openai/gpt-5.5", output_summary: "7 targets" },
      ],
    });
    const { openRunDetail } = renderPanel();
    await runSearch("pm");

    await waitFor(() => expect(screen.getByText("portfolio_manager")).toBeTruthy());
    fireEvent.click(screen.getByText("7 targets"));

    expect(openRunDetail).toHaveBeenCalledWith("run-9");
  });

  it("trade-hit symbol cell charts the symbol and does not open Run Detail", async () => {
    search.mockResolvedValue({
      query: "mrvl",
      trades: [
        { kind: "trade", id: 1, symbol: "MRVL", action: "BUY", run_id: "run-7", timestamp: "2026-08-27 13:00:00", reasoning: "Breakout continuation." },
      ],
      agent_logs: [],
    });
    const onSelectSymbol = vi.fn();
    const { openRunDetail } = renderPanel(onSelectSymbol);
    await runSearch("mrvl");

    await waitFor(() => expect(screen.getByRole("button", { name: "MRVL" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "MRVL" }));

    expect(onSelectSymbol).toHaveBeenCalledWith("MRVL");
    expect(openRunDetail).not.toHaveBeenCalled();
  });

  it("trade-hit row click on a non-symbol cell still opens Run Detail", async () => {
    search.mockResolvedValue({
      query: "mrvl",
      trades: [
        { kind: "trade", id: 1, symbol: "MRVL", action: "BUY", run_id: "run-7", timestamp: "2026-08-27 13:00:00", reasoning: "Breakout continuation." },
      ],
      agent_logs: [],
    });
    const { openRunDetail } = renderPanel();
    await runSearch("mrvl");

    await waitFor(() => expect(screen.getByText("Breakout continuation.")).toBeTruthy());
    fireEvent.click(screen.getByText("Breakout continuation."));

    expect(openRunDetail).toHaveBeenCalledWith("run-7");
  });
});
