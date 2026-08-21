import { describe, expect, it } from "vitest";
import { runsToday } from "./RunTimeline";
import { RunSummary } from "../api/client";

function run(id: string, first_timestamp: string | null): RunSummary {
  return {
    run_id: id,
    session_prefix: "run",
    first_timestamp,
    last_timestamp: first_timestamp,
    agent_count: 1,
    decision_id: null,
    total_cost_usd: null,
  };
}

// Built off the real current time rather than a hardcoded calendar date —
// runsToday() itself compares against `new Date()`, so a fixed fixture date
// would eventually go stale and always fail.
describe("runsToday", () => {
  it("keeps a run from right now and drops one from two days ago", () => {
    const now = new Date();
    const twoDaysAgo = new Date(now.getTime() - 48 * 60 * 60 * 1000);
    const runs = [run("today-run", now.toISOString()), run("old-run", twoDaysAgo.toISOString())];
    expect(runsToday(runs).map((r) => r.run_id)).toEqual(["today-run"]);
  });

  it("drops a run with no first_timestamp rather than throwing or including it", () => {
    expect(runsToday([run("no-ts", null)])).toEqual([]);
  });

  it("sorts today's runs oldest-first regardless of input order", () => {
    const now = new Date();
    const fiveMinAgo = new Date(now.getTime() - 5 * 60 * 1000);
    const runs = [run("later", now.toISOString()), run("earlier", fiveMinAgo.toISOString())];
    expect(runsToday(runs).map((r) => r.run_id)).toEqual(["earlier", "later"]);
  });
});
