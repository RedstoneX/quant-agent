// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { api } from "../api/client";

// No global setup file configures auto-cleanup, so renders would otherwise
// pile up across tests in this file (see AgentPromptViewer.test.tsx).
afterEach(cleanup);

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, journalDates: vi.fn(), journalDay: vi.fn() } };
});

const missed = api.journalDates as unknown as ReturnType<typeof vi.fn>;
const day = api.journalDay as unknown as ReturnType<typeof vi.fn>;

function stubOneMiss() {
  missed.mockResolvedValue({ dates: ["2026-08-27"] });
  day.mockResolvedValue({
    date: "2026-08-27",
    has_data: true,
    daily_pnl: null,
    reflection: {
      date: "2026-08-27",
      tomorrow_outlook: null, lessons: null, suggested_actions: null,
      risk_rating: null, tomorrow_bias: null, tomorrow_conviction: null,
      tomorrow_key_risks: null, sell_decisions_assessment: null,
      sell_grades_json: null, buy_grades_json: null,
      missed_opportunities_json: JSON.stringify([
        { symbol: "MRVL", move_pct: 6.2, miss_category: "chased_late", lesson: "Waited for confirmation." },
      ]),
      timestamp: null,
    },
    runs: [],
    trades: [],
    candidates: [],
  });
}

// Direct complaint: the owner clicks a symbol in Missed Opportunities and
// sometimes gets a Candidate Detail pop-up, sometimes doesn't, depending on
// unrelated Sessions-strip state — because the panel used to be wired to
// inspectSymbol (App.tsx), which conditionally opens a modal. This test
// pins the fix: the click only ever charts the symbol via onSelectSymbol,
// which the caller now wires to the same modal-free callback
// PositionsPanel uses (chartPositionSymbol) — this component has no
// knowledge of modals at all, so there is nothing left in it that could
// reintroduce the conditional pop-up.
describe("MissedOpportunitiesPanel row click", () => {
  it("clicking a row calls onSelectSymbol with the symbol and nothing else", async () => {
    stubOneMiss();
    const onSelectSymbol = vi.fn();
    render(<MissedOpportunitiesPanel onSelectSymbol={onSelectSymbol} />);

    await waitFor(() => expect(screen.getByText("MRVL")).toBeTruthy());
    fireEvent.click(screen.getByText("MRVL"));

    expect(onSelectSymbol).toHaveBeenCalledTimes(1);
    expect(onSelectSymbol).toHaveBeenCalledWith("MRVL");
  });

  it("renders no dialog/modal affordance — nothing in this component can open one", async () => {
    stubOneMiss();
    render(<MissedOpportunitiesPanel onSelectSymbol={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("MRVL")).toBeTruthy());
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
