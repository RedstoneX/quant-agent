// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());
import { TopStrip } from "./TopStrip";
import type { HealthResponse } from "../api/client";

// The dashboard is the owner's ONLY alert channel, so the desk status has to
// read as words next to the product name, not as a hover tooltip on a dot.
// These tests lock the three things that make that true: the label is visible
// text, it is announced to a screen reader, and the severity survives losing
// colour (weight/shape classes differ per severity).
function health(over: Partial<HealthResponse>): HealthResponse {
  return {
    status: "ok", db_reachable: true, broker_reachable: true, paper: true,
    sessions_logged_today: [], last_run_files: {}, session_lock_active: false,
    timestamp: "2026-10-01T12:00:00Z",
    alert_channel: { status: "ok" },
    ...over,
  } as HealthResponse;
}

function strip(h: HealthResponse | null) {
  return render(
    <TopStrip account={{ paper: true } as never} accountError={null} health={h} updatedAt={new Date(0)} />,
  );
}

describe("TopStrip status pill", () => {
  it("shows the health wording as visible text, once, with a status role", () => {
    strip(health({}));
    const pill = screen.getByRole("status");
    expect(pill.textContent).toContain("all systems reachable");
    expect(pill.getAttribute("aria-label")).toBe("Desk status: all systems reachable");
    expect(screen.getAllByText("all systems reachable")).toHaveLength(1);
  });

  it("keeps the broken-channel wording verbatim", () => {
    strip(health({ alert_channel: { status: "broken" } } as never));
    expect(screen.getByRole("status").textContent).toContain(
      "ALERT CHANNEL BROKEN — alarms reach nobody",
    );
  });

  it("distinguishes the severities without relying on colour", () => {
    const classesFor = (h: HealthResponse | null) => {
      const { unmount } = strip(h);
      const pill = screen.getByRole("status");
      const swatch = pill.querySelector("span[aria-hidden]") as HTMLElement;
      const out = `${pill.className} ${swatch.className}`;
      unmount();
      return out;
    };
    const fault = classesFor(health({ db_reachable: false }));
    const warnish = classesFor(health({ broker_reachable: false }));
    const ok = classesFor(health({}));
    const unknown = classesFor(null);
    expect(fault).toContain("font-bold");
    expect(fault).toContain("rounded-none");
    expect(warnish).toContain("font-semibold");
    expect(warnish).toContain("rotate-45");
    expect(ok).toContain("font-normal");
    expect(ok).toContain("rounded-full");
    expect(unknown).toContain("rounded-full");
    // Weight alone separates fault from warning from healthy in greyscale.
    expect(new Set([fault, warnish, ok]).size).toBe(3);
  });

  it("keeps the updated stamp, desk diary and legacy links", () => {
    strip(health({}));
    expect(screen.getByText(/^updated /)).toBeTruthy();
    expect(screen.getByText("Desk diary")).toBeTruthy();
    expect(screen.getByText("legacy view")).toBeTruthy();
  });

  it("lets a long label wrap instead of forcing horizontal page scroll", () => {
    strip(health({ alert_channel: { status: "broken" } } as never));
    const pill = screen.getByRole("status");
    expect(pill.className).toContain("max-w-full");
    expect((pill.querySelector("span.break-words") as HTMLElement)).toBeTruthy();
  });
});
