import { describe, expect, it } from "vitest";
import type { ResearchAgentCall, ResearchDailyResponse, StoredResearchEvidence } from "../../api/client";
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
function call(agent_name: string, output_summary: string, overrides: Partial<ResearchAgentCall> = {}): ResearchAgentCall {
  return { id: 1, agent_name, run_id: "r1", decision_id: "d1", timestamp: "2026-08-25T15:00:00Z", status: "success", output_summary,
    requested_provider: "openrouter", requested_model: "model", actual_provider: "openrouter", model: "model", prompt_version: "v1", latency_s: 1, cost_usd: .01, structured_evidence_count: 0, ...overrides };
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
    expect(data.tension).toContain("Technical");
    expect(data.tension).toContain("Macro");
    expect(data.signal_stack.find((signal) => signal.seat === "technical")?.direction).toBe("bullish");
  });

  it("maps canonical SEC findings and admissions while excluding scan rows", () => {
    const finding = evidence({ agent_name: "smart_money_analyst", kind: "finding", symbol: "SPIR", payload: {
      stance: "bullish", economic_role: "actionable", summary: "Two independent insiders bought common stock.", why_now: "Accepted yesterday after open-market purchases.",
      observations: [{ stream: "insider", actor: "Owner One", direction: "buy", transaction_date: "2026-08-22", accepted_at: "2026-08-24T17:30:15Z", lag_days: 2, freshness: "fresh", accession_number: "0001", source_url: "https://www.sec.gov/Archives/0001.txt", transient_admitted: true }],
    } });
    const admission = evidence({ id: 2, agent_name: "smart_money_analyst", kind: "admission", symbol: "SPIR", payload: { temporary: true, reason: "material_sec_form4_purchase", sector: "Industrials", transaction_value_usd: 251250 } });
    const scan = evidence({ id: 3, agent_name: "smart_money_analyst", kind: "scan_summary", symbol: null, payload: { source: "SEC Form 4", state: "material", findings: 1 } });
    const data = buildResearchDesk(response([finding, admission, scan]));
    expect(data.smart_money).toHaveLength(1);
    expect(data.smart_money[0]).toMatchObject({ classification: "actionable", direction: "bullish", freshness: "timely", event_timestamp: "2026-08-22", knowable_timestamp: "2026-08-24T17:30:15Z", lag_days: 2, source_name: "SEC Form 4", admitted_this_run: true });
    expect(data.smart_money[0].source_detail).toContain("SEC accession");
    expect(data.smart_money[0].admission_detail).toContain("Industrials");
  });

  it("keeps a run-scoped SEC admission visible when paid synthesis stored no finding", () => {
    const admission = evidence({ agent_name: "smart_money_analyst", kind: "admission", symbol: "SPIR", payload: {
      temporary: true, reason: "material_sec_form4_purchase", sector: "Industrials", transaction_value_usd: 251250,
      accessions: ["0001"], owners: ["Owner One"],
    } });
    const data = buildResearchDesk(response([admission]));
    expect(data.smart_money).toHaveLength(1);
    expect(data.smart_money[0]).toMatchObject({ classification: "admission", admitted_this_run: true, symbol: "SPIR", direction: "bullish" });
    expect(data.smart_money[0].summary).toContain("Paid synthesis is not required");
  });

  it("maps actual review agent names without relabeling evening lessons as meta-reflection", () => {
    const raw = response();
    raw.runs[0].agent_calls = [call("evening_analyst", "Execution stayed inside the plan."), call("meta_reflector", "Size rules beat narrative confidence.", { id: 2 })];
    const data = buildResearchDesk(raw);
    expect(data.reviews?.evening_review).toBe("Execution stayed inside the plan.");
    expect(data.reviews?.meta_reflection).toBe("Size rules beat narrative confidence.");
  });

  it("derives evidence-backed changes against the prior useful day", () => {
    const raw = response([evidence({ payload: { rating: "buy", reasoning: "Support held.", why_now: "Price retested support today.", entry_price: 101, stop_loss: 98, reference_target: 110 } })]);
    const prior = response([evidence({ run_id: "r0", timestamp: "2026-08-24T13:00:00Z", payload: { rating: "neutral", reasoning: "Setup had not confirmed." } })]);
    prior.date = "2026-08-24"; prior.as_of = "2026-08-24T13:00:00Z";
    prior.freshness = { latest_recorded_at: "2026-08-24T13:00:00Z", age_minutes: null, label: "historical" };
    const data = buildResearchDesk(raw, prior);
    expect(data.what_changed[0]).toContain("neutral → bullish");
    expect(data.why_now).toBe("Price retested support today.");
    expect(data.prior_as_of).toBe("2026-08-24T13:00:00Z");
    expect(data.dry_annotation).toBe("Several reads. No portfolio instruction. That is still a decision.");
    expect(data.agents.find((agent) => agent.seat === "technical")?.market_context[0]).toMatchObject({ stop: 98, entry: 101, target: 110 });
  });

  it("uses explicit News state changes for changed and why-now copy", () => {
    const data = buildResearchDesk(response([evidence({ agent_name: "news_analyst", symbol: null, payload: {
      market_sentiment: "neutral", pm_briefing: "Rate-sensitive sectors need a tighter read.",
      state_changes: [{ event: "Fed signals a pause", previous_state: "Hiking", new_state: "Paused", market_impact: "Duration pressure eased." }],
    } })]));
    expect(data.what_changed[0]).toBe("News: Fed signals a pause · Hiking → Paused · Duration pressure eased.");
    expect(data.why_now).toBe("Duration pressure eased.");
  });

  it("prefers structured evidence and formats canonical PM, Risk, gate and actual deltas", () => {
    const raw = response([evidence({ agent_name: "macro_analyst", kind: "analysis", symbol: null, payload: { regime: "transitional", equity_outlook: "neutral", summary: "Breadth remains narrow; keep exposure selective." } })]);
    raw.runs[0].agent_calls = [call("macro_analyst", "regime=transitional, outlook=neutral")];
    raw.runs[0].decision_delta = { run_id: "r1", decision_id: "d1", state: "executed",
      proposed: [evidence({ agent_name: "portfolio_manager", kind: "target", symbol: "AAPL", payload: { target_weight_pct: 8, conviction: "medium", thesis: "Bounded long." } })],
      risk_changes: [
        evidence({ id: 2, agent_name: "risk_manager", kind: "verdict", symbol: null, payload: { approved: true, reason_category: "concentration", reasoning: "Approved after resizing." } }),
        evidence({ id: 3, agent_name: "risk_manager", kind: "modification", payload: { symbol: "AAPL", original_value: 10, new_value: 8, reason: "Concentration" } }),
      ],
      deterministic_events: [evidence({ id: 4, agent_name: "pipeline", kind: "pipeline_event", payload: { stage: "deterministic_gate", outcome: "allowed", reason: "post_risk_checks_passed" } })],
      trades: [{ id: 5, symbol: "AAPL", action: "BUY", qty: 4, price: 221, reasoning: "Grounded order.", run_id: "r1", decision_id: "d1", fill_status: "filled", fill_qty: 4, fill_price: 221.1, timestamp: "2026-08-25T15:05:00Z", stop_loss: 218, take_profit: 230 }],
    };
    raw.runs.push({ summary: { ...raw.runs[0].summary, run_id: "r2", decision_id: null, last_timestamp: "2026-08-25T16:00:00Z" }, agent_calls: [],
      evidence: [evidence({ id: 20, run_id: "r2", decision_id: null, agent_name: "pipeline", kind: "pipeline_event", timestamp: "2026-08-25T16:00:00Z", payload: { stage: "specialist_complete" } })],
      decision_delta: { run_id: "r2", decision_id: null, state: "no_proposal", proposed: [], risk_changes: [], deterministic_events: [], trades: [] } });
    const data = buildResearchDesk(raw);
    expect(data.agents.find((agent) => agent.seat === "macro")?.read).toBe("Breadth remains narrow; keep exposure selective.");
    expect(data.decision_chain.map((step) => step.summary)).toEqual(expect.arrayContaining(["1 target: AAPL 8%", "Approved with 1 change.", "allowed · post risk checks passed", "1 stored trade: BUY 4 AAPL @ 221.1"]));
    expect(data.decision_run_id).toBe("r1");
  });

  it("edits after-bell P&L, lessons, actions and JSON risks into separate fields", () => {
    const raw = response();
    raw.daily_pnl = { date: raw.date, total_value: 100250, daily_pnl: 250, daily_return_pct: .25, equity_close: 100250 };
    raw.reflection = { date: raw.date, tomorrow_outlook: "Watch support before adding.", lessons: "Patience added more than prediction.", suggested_actions: "[\"Keep size bounded.\"]", risk_rating: "medium", tomorrow_bias: "neutral", tomorrow_conviction: "medium", tomorrow_key_risks: "[\"Breadth\",\"Concentration\"]", sell_decisions_assessment: null, sell_grades_json: null, buy_grades_json: null, missed_opportunities_json: null, timestamp: raw.as_of };
    const data = buildResearchDesk(raw);
    expect(data.reviews).toMatchObject({ daily_result: "+$250 · +0.25% · equity $100,250", lesson_learned: "Patience added more than prediction.", suggested_actions: ["Keep size bounded."], tomorrow_watch: ["Watch support before adding.", "Breadth", "Concentration"] });
  });

  it("names partial and provider-error states instead of blanking the desk", () => {
    const raw = response(); raw.state = "partial"; raw.read_error = "smart-money provider unavailable"; raw.missing_sources = ["smart_money"];
    const data = buildResearchDesk(raw);
    expect(data.status).toBe("partial");
    expect(data.errors).toEqual(["smart-money provider unavailable", "Missing source: smart_money"]);
  });
});
