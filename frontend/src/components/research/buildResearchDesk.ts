import type {
  ResearchAgentBrief, ResearchDailyResponse, ResearchDecisionStep, ResearchDeskData, ResearchDirection,
  ResearchEvidenceItem, ResearchItemStatus, SmartMoneyFinding, StoredResearchEvidence,
} from "../../api/client";

const SEATS = ["technical", "news", "macro", "earnings", "smart_money", "portfolio_manager", "ai_risk", "position_reviewer", "evening_review", "meta_reflection"];
const aliases: Record<string, string> = {
  tech_analyst: "technical", technical_analyst: "technical", news_analyst: "news", macro_analyst: "macro",
  earnings_analyst: "earnings", smart_money_analyst: "smart_money", risk_manager: "ai_risk",
  ai_risk_manager: "ai_risk", portfolio_manager: "portfolio_manager", position_reviewer: "position_reviewer",
  evening_review: "evening_review", meta_reflection: "meta_reflection",
};

function seatOf(value: string) { const key = value.toLowerCase().replace(/ /g, "_"); return aliases[key] || key; }
function object(value: unknown): Record<string, unknown> | null { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function stringAt(payload: unknown, ...keys: string[]): string | null {
  const root = object(payload); if (!root) return null;
  for (const key of keys) { const value = root[key]; if (typeof value === "string" && value.trim()) return value.trim(); }
  for (const value of Object.values(root)) { const nested = object(value); if (nested) { const found = stringAt(nested, ...keys); if (found) return found; } }
  return null;
}
function numberAt(payload: unknown, ...keys: string[]): number | null {
  const root = object(payload); if (!root) return null;
  for (const key of keys) if (typeof root[key] === "number") return root[key] as number;
  return null;
}
function directionOf(payload: unknown): ResearchDirection {
  const value = stringAt(payload, "direction", "rating", "sentiment", "equity_outlook", "outlook")?.toLowerCase();
  if (!value) return "unknown"; if (value.includes("bull") || value.includes("buy") || value.includes("long")) return "bullish";
  if (value.includes("bear") || value.includes("sell") || value.includes("short")) return "bearish";
  if (value.includes("mixed")) return "mixed"; if (value.includes("neutral")) return "neutral"; return "unknown";
}
function evidenceChips(rows: StoredResearchEvidence[]): ResearchEvidenceItem[] {
  return rows.filter((row) => row.state === "valid").slice(0, 5).map((row) => ({
    label: row.symbol || row.kind.replace(/_/g, " "), value: stringAt(row.payload, "rating", "sentiment", "verdict", "status", "regime", "action") || row.scope,
    source: row.agent_name, timestamp: row.timestamp,
  }));
}
function rowSummary(row: StoredResearchEvidence | undefined): string | null {
  if (!row) return null;
  return stringAt(row.payload, "summary", "reasoning", "pm_briefing", "key_thesis", "thesis", "impact_summary", "headline");
}
function statusFor(data: ResearchDailyResponse): ResearchItemStatus {
  if (data.state === "error") return "error"; if (data.state === "empty") return "quiet"; if (data.state === "partial") return "partial";
  if (data.freshness.label === "stale" || data.freshness.label === "historical") return "stale"; return "current";
}
function smartMoney(rows: StoredResearchEvidence[]): SmartMoneyFinding[] {
  return rows.filter((row) => seatOf(row.agent_name) === "smart_money" && row.state === "valid").map((row) => {
    const payload = object(row.payload) || {};
    const classification = stringAt(payload, "classification", "actionability")?.toLowerCase();
    const freshness = stringAt(payload, "freshness")?.toLowerCase();
    return {
      id: String(row.id), symbol: row.symbol, stream: stringAt(payload, "stream", "source_type", "data_type") || row.kind.replace(/_/g, " "),
      headline: stringAt(payload, "headline", "title") || `${row.symbol || "Market"} smart-money finding`,
      summary: rowSummary(row) || "Structured finding persisted without a narrative summary.",
      classification: (["actionable", "confirmatory", "contradictory", "historical"].includes(classification || "") ? classification : "historical") as SmartMoneyFinding["classification"],
      freshness: (["real_time", "timely", "delayed", "stale", "unknown"].includes(freshness || "") ? freshness : "unknown") as SmartMoneyFinding["freshness"],
      event_timestamp: stringAt(payload, "event_timestamp", "transaction_date", "transaction_timestamp", "filed_at"),
      knowable_timestamp: stringAt(payload, "knowable_timestamp", "disclosure_date", "disclosed_at", "filing_date") || row.timestamp,
      lag_days: numberAt(payload, "lag_days", "disclosure_lag_days"), materiality: stringAt(payload, "materiality", "why_it_matters", "why_now"),
      source_name: stringAt(payload, "source_name", "provider") || "Stored source attribution unavailable",
      source_url: stringAt(payload, "source_url", "url"), source_detail: stringAt(payload, "provenance", "source_detail"),
    };
  });
}

export function buildResearchDesk(data: ResearchDailyResponse): ResearchDeskData {
  const allEvidence = data.runs.flatMap((run) => run.evidence);
  const allCalls = data.runs.flatMap((run) => run.agent_calls);
  const agents: ResearchAgentBrief[] = SEATS.map((seat) => {
    const calls = allCalls.filter((call) => seatOf(call.agent_name) === seat).sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)));
    const rows = allEvidence.filter((row) => seatOf(row.agent_name) === seat);
    const valid = rows.filter((row) => row.state === "valid"); const latest = valid[valid.length - 1]; const call = calls[0];
    return { seat, status: call?.status === "error" || rows.some((r) => r.state === "invalid") ? "error" : call || latest ? statusFor(data) : "unavailable",
      headline: stringAt(latest?.payload, "headline", "key_thesis", "title") || call?.output_summary || null,
      read: call?.output_summary || rowSummary(latest), direction: directionOf(latest?.payload), evidence: evidenceChips(valid), changed: null,
      tension: null, why_now: stringAt(latest?.payload, "why_now", "catalyst"), timestamp: call?.timestamp || latest?.timestamp || null,
      error: call?.status === "error" ? "Agent call failed; no conclusion is inferred." : rows.some((r) => r.state === "invalid") ? "Some structured evidence was invalid." : null };
  });
  const explicit = agents.filter((agent) => agent.direction !== "unknown" && agent.direction !== "neutral");
  const directions = new Set(explicit.map((agent) => agent.direction));
  const tension = directions.has("bullish") && directions.has("bearish") ? `${explicit.filter(a => a.direction === "bullish").map(a => a.seat).join(", ")} read bullish while ${explicit.filter(a => a.direction === "bearish").map(a => a.seat).join(", ")} read bearish.` : null;
  const signals = agents.filter((agent) => ["technical", "news", "macro", "earnings", "smart_money"].includes(agent.seat)).map((agent) => ({ seat: agent.seat, direction: agent.direction, signal: agent.headline || agent.read,
    relationship: agent.direction === "unknown" || agent.direction === "neutral" ? "independent" as const : directions.size > 1 ? "conflicts" as const : "agrees" as const, timestamp: agent.timestamp }));
  const steps: ResearchDecisionStep[] = [];
  const delta = data.runs.map((r) => r.decision_delta).find((d) => d.decision_id) || data.runs[data.runs.length - 1]?.decision_delta;
  if (delta) {
    const pm = delta.proposed[delta.proposed.length - 1], risk = delta.risk_changes[delta.risk_changes.length - 1], gate = delta.deterministic_events[delta.deterministic_events.length - 1], trade = delta.trades[delta.trades.length - 1];
    steps.push({ stage: "read", status: "stored", summary: agents.find(a => a.seat === "portfolio_manager")?.read || "Specialist evidence reached the decision chain.", detail: null, timestamp: agents.find(a => a.seat === "portfolio_manager")?.timestamp || null });
    steps.push({ stage: "portfolio_manager", status: pm ? "proposed" : "no proposal", summary: rowSummary(pm), detail: pm?.kind || null, timestamp: pm?.timestamp || null });
    steps.push({ stage: "ai_risk", status: risk ? "reviewed" : "not recorded", summary: rowSummary(risk), detail: risk?.kind || null, timestamp: risk?.timestamp || null });
    steps.push({ stage: "deterministic_gate", status: delta.state === "hard_risk_block" ? "blocked" : gate ? "recorded" : "not recorded", summary: rowSummary(gate), detail: gate?.kind || null, timestamp: gate?.timestamp || null });
    steps.push({ stage: "execution", status: trade ? trade.fill_status || trade.action : "not executed", summary: trade ? `${trade.action} ${trade.qty ?? "—"} ${trade.symbol}${trade.fill_price != null ? ` @ ${trade.fill_price}` : ""}` : "No execution was stored.", detail: trade?.reasoning || null, timestamp: trade?.timestamp || null });
  }
  const pmRead = agents.find((a) => a.seat === "portfolio_manager" && a.read)?.read;
  const latestRead = [...agents].reverse().find((a) => a.read)?.read;
  return { date: data.date, status: statusFor(data), as_of: data.as_of, prior_as_of: null, thesis: pmRead || latestRead || null, what_changed: [], tension,
    why_now: null, dry_annotation: null, agents, signal_stack: signals, decision_chain: steps, smart_money: smartMoney(allEvidence),
    reviews: { position_reviewer: agents.find(a => a.seat === "position_reviewer")?.read || null, evening_review: agents.find(a => a.seat === "evening_review")?.read || null,
      meta_reflection: agents.find(a => a.seat === "meta_reflection")?.read || data.reflection?.lessons || null,
      tomorrow_watch: [data.reflection?.tomorrow_outlook, data.reflection?.tomorrow_key_risks].filter((x): x is string => Boolean(x)) },
    errors: [data.read_error, ...data.missing_sources.map((source) => `Missing source: ${source}`)].filter((x): x is string => Boolean(x)) };
}
