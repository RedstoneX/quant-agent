import { describe, expect, it } from "vitest";
import { etDateKey } from "./format";

describe("etDateKey", () => {
  it("converts a UTC evening timestamp to the same ET calendar day", () => {
    // 2026-08-21 15:00 UTC = 2026-08-21 11:00 EDT (UTC-4) — no day rollover.
    expect(etDateKey("2026-08-21 15:00:00")).toBe("2026-08-21");
  });

  it("rolls a late-UTC timestamp back to the PRIOR ET calendar day", () => {
    // 2026-08-21 02:00 UTC = 2026-08-20 22:00 EDT — the classic case a
    // naive UTC-date-string comparison would get wrong (the Cockpit's
    // today's-runs timeline and the price chart's staleness label both
    // depend on this being correct).
    expect(etDateKey("2026-08-21T02:00:00Z")).toBe("2026-08-20");
  });

  it("accepts a Date object directly", () => {
    const d = new Date("2026-08-21T15:00:00Z");
    expect(etDateKey(d)).toBe("2026-08-21");
  });

  it("returns an empty string for an unparseable input rather than throwing", () => {
    expect(etDateKey("not-a-timestamp")).toBe("");
  });
});
