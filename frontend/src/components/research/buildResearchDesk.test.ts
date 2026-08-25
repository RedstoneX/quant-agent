import { describe, expect, it } from "vitest";
import type { ResearchDailyResponse, StoredResearchEvidence } from "../../api/client";
import { buildResearchDesk } from "./buildResearchDesk";

function evidence(overrides: Partial<StoredResearchEvidence>): StoredResearchEvidence {
  return { id: 1, run_id: "r1", decision_id: "d1", agent_name: "tech_analyst", kind: "technical_analysis",
    scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:00:00Z", state: "valid", payload: {}, ...overrides };
}
function response(rows: StoredResearchEvidence[] = []): ResearchDailyResponse {
  return { date: "2026-08-25", as_of: "2026-08-25T20:00:00Z", state: "complete", freshness: { latest_recorded_at: "2026-08-25T20:00:00Z", age_minutes: 2, label: "current" },
    read_error: null, missing_sources: [], daily_pnl: null, reflection: null, runs: [{ summary: { run_id: "r1", session_prefix: "morning", first_timestamp: null, last_timestamp: null, agent_count: 0, decision_id: "d1", total_cost_usd: null }, agent_calls: [], evidence: rows,
      decision_delta: { run_id: "r1", decision_id: "d1", state: "no_proposal", proposed: [], risk_changes: [], deterministic_events: [], trades: [] } }] };
}

describe("buildResearchDesk", () => {
  it("always represents every required research and review seat without fabricating reads", () => {
    const data = buildResearchDesk(response());
    expect(data.agents.map((agent) => agent.seat)).toEqual(expect.arrayContaining(["technical", "news", "macro", "earnings", "smart_money", "portfolio_manager", "ai_risk", "position_reviewer", "evening_review", "meta_reflection"]));
    expect(data.agents.every((agent) => agent.status === "unavailable")).toBe(true);
    expect(data.thesis).toBeNull();
    expect(data.what_changed).toEqual([]);
  });

  it("derives disagreement only from explicit structured directions", () => {
    const data = buildResearchDesk(response([
      evidence({ payload: { rating: "buy", reasoning: "Trend holds" } }),
      evidence({ id: 2, agent_name: "macro_analyst", kind: "macro", symbol: null, payload: { equity_outlook: "bearish", summary: "Liquidity is tightening" } }),
    ]));
    expect(data.tension).toContain("technical");
    expect(data.tension).toContain("macro");
    expect(data.signal_stack.find((signal) => signal.seat === "technical")?.direction).toBe("bullish");
  });

  it("preserves smart-money happened/knowable timestamps, lag and provenance", () => {
    const data = buildResearchDesk(response([evidence({ agent_name: "smart_money_analyst", kind: "congressional_trade", payload: {
      headline: "Clustered purchases", summary: "Three related purchases were disclosed.", classification: "confirmatory", freshness: "stale",
      transaction_date: "2026-07-10T00:00:00Z", disclosure_date: "2026-08-24T00:00:00Z", lag_days: 45,
      provider: "Bargo", source_url: "https://example.test/source", provenance: "House STOCK Act disclosure",
    } })]));
    expect(data.smart_money[0]).toMatchObject({ classification: "confirmatory", freshness: "stale", event_timestamp: "2026-07-10T00:00:00Z", knowable_timestamp: "2026-08-24T00:00:00Z", lag_days: 45, source_name: "Bargo" });
  });

  it("names partial and provider-error states instead of blanking the desk", () => {
    const raw = response(); raw.state = "partial"; raw.read_error = "smart-money provider unavailable"; raw.missing_sources = ["smart_money"];
    const data = buildResearchDesk(raw);
    expect(data.status).toBe("partial");
    expect(data.errors).toEqual(["smart-money provider unavailable", "Missing source: smart_money"]);
  });
});
