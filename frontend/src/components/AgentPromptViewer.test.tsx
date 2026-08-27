// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { AgentPromptViewer } from "./AgentPromptViewer";
import { AgentLogItem } from "../api/client";

/* `agent_logs.input_message` has stored the complete assembled prompt since
 * the column was added, and `/runs/{run_id}` has served it via `SELECT *`
 * the whole time — verified against the live paper database on 2026-08-27,
 * where all 32 Portfolio Manager rows carry it at 13KB-190KB each. The gap
 * was never persistence or the API. It was that `AgentLogItem` in
 * api/client.ts did not declare the field, so no view could reach it.
 *
 * This is the operator's "what the PM actually read" view: the assembled
 * briefing — seven-evening narrative, recurring missed themes, repeat loss
 * patterns, recent risk verdicts, the PM's own last decisions, win-rate
 * calibration — rather than the source material it was built from. */

function log(overrides: Partial<AgentLogItem> = {}): AgentLogItem {
  return {
    id: 1, agent_name: "portfolio_manager", decision_id: null,
    timestamp: "2026-08-27 13:34:40",
    input_summary: "30 candidates", input_message: null,
    output_summary: "7 targets", full_response: null,
    requested_provider: "openrouter", requested_model: "openai/gpt-5.5",
    actual_provider: "openrouter", model: "openai/gpt-5.5",
    status: "ok", cost_usd: 0.11, latency_s: 43, tokens_used: 1000,
    ...overrides,
  };
}

describe("AgentPromptViewer", () => {
  // No global setup file configures auto-cleanup, so renders would otherwise
  // accumulate in document.body and role queries would match across tests.
  afterEach(cleanup);

  it("renders the prompt verbatim once expanded", () => {
    const prompt = "## Account Status\n- Total Value: $9,880.03\n### Recent RM verdicts\napproved";
    const { getByText, container } = render(
      <AgentPromptViewer log={log({ input_message: prompt })} />,
    );
    // Open by default — the prompt is the point of the view.
    expect(container.querySelector("pre")?.textContent).toBe(prompt);
    expect(getByText(/Prompt the agent received/)).toBeTruthy();
  });

  it("collapses to a preview and toggles back open", () => {
    const prompt = "A".repeat(5000);
    const { container, getByRole } = render(
      <AgentPromptViewer log={log({ input_message: prompt })} />,
    );
    const toggle = getByRole("button", { name: /Prompt the agent received/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Collapsed: no full text in the DOM, so a 190KB prompt cannot blow up
    // the modal just by being present in the run.
    expect(container.querySelector("pre")).toBeNull();
  });

  it("reports size, because a 190KB prompt is itself a finding", () => {
    const { getByText } = render(
      <AgentPromptViewer log={log({ input_message: "A".repeat(2048) })} />,
    );
    expect(getByText("2.0KB")).toBeTruthy();
  });

  it("shows the raw response as a separate collapsed section", () => {
    const { getByRole } = render(
      <AgentPromptViewer log={log({ input_message: "prompt", full_response: "{}" })} />,
    );
    expect(getByRole("button", { name: /Raw response/ }).getAttribute("aria-expanded"))
      .toBe("false");
  });

  it("explains an empty row rather than rendering a blank panel", () => {
    // Rows written before the column existed, and calls that failed before
    // the prompt was assembled, are legitimately empty — that is not a bug
    // and the operator should not be left guessing whether it is.
    const { getByText } = render(<AgentPromptViewer log={log()} />);
    expect(getByText(/No prompt or response was persisted/)).toBeTruthy();
  });

  it("still renders when only the response survived", () => {
    const { getByRole, queryByRole } = render(
      <AgentPromptViewer log={log({ full_response: "{\"targets\": []}" })} />,
    );
    expect(getByRole("button", { name: /Raw response/ })).toBeTruthy();
    expect(queryByRole("button", { name: /Prompt the agent received/ })).toBeNull();
  });
});
