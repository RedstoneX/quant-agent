import type {
  ResearchAgentBrief, ResearchDailyResponse, ResearchDecisionDeltaRaw, ResearchDecisionStep,
  ResearchDeskData, ResearchDirection, ResearchEvidenceItem, ResearchItemStatus, ResearchRun,
  ResearchMarketContext, SmartMoneyFinding, StoredResearchEvidence,
} from "../../api/client";

const SEATS = [
  "technical", "news", "macro", "earnings", "smart_money", "portfolio_manager",
  "ai_risk", "position_reviewer", "evening_review", "meta_reflection",
];
const SIGNAL_SEATS = new Set(["technical", "news", "macro", "earnings", "smart_money"]);
const aliases: Record<string, string> = {
  tech_analyst: "technical", technical_analyst: "technical", news_analyst: "news",
  macro_analyst: "macro", earnings_analyst: "earnings", smart_money_analyst: "smart_money",
  risk_manager: "ai_risk", ai_risk_manager: "ai_risk", portfolio_manager: "portfolio_manager",
  position_reviewer: "position_reviewer", evening_analyst: "evening_review",
  evening_review: "evening_review", meta_reflector: "meta_reflection",
  meta_reflection: "meta_reflection",
};
const seatNames: Record<string, string> = {
  technical: "Technical", news: "News", macro: "Macro", earnings: "Earnings",
  smart_money: "Smart Money", portfolio_manager: "PM", ai_risk: "Risk",
  position_reviewer: "Position review", evening_review: "Evening review",
  meta_reflection: "Meta-reflection",
};

function seatOf(value: string) {
  const key = value.toLowerCase().trim().replace(/\s+/g, "_");
  return aliases[key] || key;
}
function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function objects(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(object).filter((item): item is Record<string, unknown> => Boolean(item)) : [];
}
function last<T>(items: T[]): T | undefined { return items[items.length - 1]; }
function stringAt(payload: unknown, ...keys: string[]): string | null {
  const root = object(payload);
  if (!root) return null;
  for (const key of keys) {
    const value = root[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  for (const value of Object.values(root)) {
    const nested = object(value);
    if (nested) { const found = stringAt(nested, ...keys); if (found) return found; }
  }
  return null;
}
function numberAt(payload: unknown, ...keys: string[]): number | null {
  const root = object(payload);
  if (!root) return null;
  for (const key of keys) if (typeof root[key] === "number" && Number.isFinite(root[key])) return root[key] as number;
  return null;
}
function booleanAt(payload: unknown, ...keys: string[]): boolean | null {
  const root = object(payload);
  if (!root) return null;
  for (const key of keys) if (typeof root[key] === "boolean") return root[key] as boolean;
  return null;
}
function editCopy(value: string | null | undefined, max = 320): string | null {
  if (!value) return null;
  const clean = value
    .replace(/```(?:json)?/gi, "")
    .replace(/^\s*(?:analysis|summary|conclusion|overall|output)\s*:\s*/i, "")
    .replace(/^\s*(?:as an ai[^,.]*[,.:]?\s*)/i, "")
    .replace(/[\t\r\n]+/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
  if (!clean) return null;
  if (clean.length <= max) return clean;
  const clipped = clean.slice(0, max + 1);
  const sentence = Math.max(clipped.lastIndexOf(". "), clipped.lastIndexOf("; "));
  const space = clipped.lastIndexOf(" ");
  return `${clipped.slice(0, sentence > max * .55 ? sentence + 1 : Math.max(space, max * .7)).trim()}…`;
}
function firstSentence(value: string | null, max = 108): string | null {
  const clean = editCopy(value, 240);
  if (!clean) return null;
  const end = clean.search(/[.!?](?:\s|$)/);
  return editCopy(end >= 0 ? clean.slice(0, end + 1) : clean, max);
}
function failureHeadline(seat: string): string {
  if (seat === "portfolio_manager") return "No valid Portfolio Manager decision";
  if (seat === "ai_risk") return "No valid AI Risk verdict";
  return `No valid ${seatNames[seat] || seat} read`;
}
function storedList(value: string | null | undefined): string[] {
  if (!value?.trim()) return [];
  try {
    const parsed: unknown = JSON.parse(value.trim());
    if (Array.isArray(parsed)) return parsed.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => editCopy(item, 220)).filter((item): item is string => Boolean(item));
  } catch { /* older rows store ordinary prose */ }
  const clean = editCopy(value, 1200);
  return clean ? [clean] : [];
}
function dailyResult(data: ResearchDailyResponse): string | null {
  const point = data.daily_pnl;
  if (!point) return null;
  const parts: string[] = [];
  if (point.daily_pnl != null) parts.push(`${point.daily_pnl >= 0 ? "+" : "−"}$${Math.abs(point.daily_pnl).toLocaleString("en-US", { maximumFractionDigits: 2 })}`);
  if (point.daily_return_pct != null) parts.push(`${point.daily_return_pct >= 0 ? "+" : ""}${point.daily_return_pct.toFixed(2)}%`);
  if (point.equity_close != null) parts.push(`equity $${point.equity_close.toLocaleString("en-US", { maximumFractionDigits: 0 })}`);
  return parts.join(" · ") || null;
}
function directionOf(payload: unknown): ResearchDirection {
  const value = stringAt(payload, "stance", "direction", "rating", "sentiment", "market_sentiment", "equity_outlook", "outlook")?.toLowerCase();
  if (!value) return "unknown";
  if (value.includes("bull") || value === "buy" || value === "long") return "bullish";
  if (value.includes("bear") || value === "sell" || value === "short") return "bearish";
  if (value.includes("mixed")) return "mixed";
  if (value.includes("neutral") || value === "hold") return "neutral";
  return "unknown";
}
function rowSummary(row: StoredResearchEvidence | undefined): string | null {
  if (!row) return null;
  return editCopy(stringAt(
    row.payload, "portfolio_view", "summary", "pm_briefing", "key_thesis", "thesis",
    "impact_summary", "reasoning", "overall", "headline", "lesson",
  ), 240);
}
function sortEvidence(rows: StoredResearchEvidence[]) {
  return [...rows].sort((a, b) => `${a.timestamp || ""}:${a.id}`.localeCompare(`${b.timestamp || ""}:${b.id}`));
}
function representative(rows: StoredResearchEvidence[], seat: string): StoredResearchEvidence | undefined {
  const priority: Record<string, string[]> = {
    smart_money: ["finding"], portfolio_manager: ["reasoning", "target", "proposed_order"],
    ai_risk: ["verdict", "modification"],
  };
  const sorted = sortEvidence(rows);
  for (const kind of priority[seat] || ["analysis"]) {
    const match = [...sorted].reverse().find((row) => row.kind === kind);
    if (match) return match;
  }
  return sorted[sorted.length - 1];
}
function statusFor(data: ResearchDailyResponse): ResearchItemStatus {
  if (data.state === "error") return "error";
  if (data.state === "empty") return "quiet";
  if (data.state === "partial") return "partial";
  if (data.freshness.label === "aging") return "aging";
  if (data.freshness.label === "stale") return "stale";
  if (data.freshness.label === "historical") return "historical";
  return "current";
}
function evidenceValue(row: StoredResearchEvidence): string {
  if (row.kind === "admission") return "admitted this run";
  if (row.kind === "scan_summary") {
    const observations = numberAt(row.payload, "observations"); const findings = numberAt(row.payload, "findings");
    return [observations != null ? `${observations} obs` : null, findings != null ? `${findings} finding${findings === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ") || "quiet";
  }
  if (row.kind === "finding") return [stringAt(row.payload, "economic_role"), stringAt(row.payload, "stance")].filter(Boolean).join(" · ") || "material";
  if (seatOf(row.agent_name) === "macro") return [stringAt(row.payload, "regime"), stringAt(row.payload, "equity_outlook"), stringAt(row.payload, "confidence")].filter(Boolean).join(" · ") || "macro read";
  if (seatOf(row.agent_name) === "news") return [stringAt(row.payload, "market_sentiment"), stringAt(row.payload, "confidence")].filter(Boolean).join(" · ") || "news read";
  const primary = stringAt(row.payload, "rating", "market_sentiment", "sentiment", "equity_outlook", "regime", "stance", "action", "outcome", "state", "reason_category");
  const secondary = stringAt(row.payload, "conviction", "freshness");
  if (primary) return [primary, secondary].filter(Boolean).join(" · ");
  const target = numberAt(row.payload, "target_weight_pct", "allocation_pct");
  if (target != null) return `${target}% target`;
  const approved = booleanAt(row.payload, "approved");
  if (approved != null) return approved ? "approved" : "rejected";
  return row.kind.replace(/_/g, " ");
}
function evidenceChips(rows: StoredResearchEvidence[]): ResearchEvidenceItem[] {
  return sortEvidence(rows).reverse().slice(0, 6).map((row) => ({
    label: row.kind === "scan_summary" ? "SEC scan" : row.kind === "admission" ? row.symbol || "Admission" : row.symbol || row.kind.replace(/_/g, " "), value: evidenceValue(row),
    source: row.agent_name, timestamp: row.timestamp,
  }));
}
function marketContext(rows: StoredResearchEvidence[]): ResearchMarketContext[] {
  return sortEvidence(rows).reverse().flatMap((row) => {
    const entry = numberAt(row.payload, "entry_price");
    const stop = numberAt(row.payload, "stop_loss", "suggested_stop_price");
    const target = numberAt(row.payload, "reference_target", "take_profit");
    if (!row.symbol || entry == null || stop == null || target == null || !(stop < entry && entry < target)) return [];
    return [{ symbol: row.symbol, stop, entry, target }];
  }).slice(0, 3);
}
function aggregateDirection(rows: StoredResearchEvidence[]): ResearchDirection {
  const directions = rows.map((row) => directionOf(row.payload)).filter((value) => value !== "unknown" && value !== "neutral");
  const unique = new Set(directions);
  if (unique.size > 1 || unique.has("mixed")) return "mixed";
  return directions[0] || directionOf(representative(rows, "")?.payload);
}
function structuredHeadline(seat: string, rows: StoredResearchEvidence[], summary: string | null): string | null {
  const explicit = editCopy(stringAt(representative(rows, seat)?.payload, "headline", "title"), 108);
  if (explicit) return explicit;
  const direction = aggregateDirection(rows);
  if ((seat === "technical" || seat === "earnings") && rows.length > 1) {
    const counts = rows.reduce<Record<string, number>>((out, row) => {
      const key = directionOf(row.payload); out[key] = (out[key] || 0) + 1; return out;
    }, {});
    const parts = ["bullish", "neutral", "bearish"].filter((key) => counts[key]).map((key) => `${counts[key]} ${key}`);
    if (parts.length) return `${rows.length} ${seat === "technical" ? "setups" : "filing reads"} · ${parts.join(" · ")}`;
  }
  if (seat === "macro") {
    const payload = representative(rows, seat)?.payload;
    const regime = stringAt(payload, "regime"); const outlook = stringAt(payload, "equity_outlook");
    if (regime || outlook) return [regime && `${regime} regime`, outlook && `${outlook} outlook`].filter(Boolean).join(" · ");
  }
  if (seat === "ai_risk") {
    const verdict = rows.find((row) => row.kind === "verdict");
    const approved = booleanAt(verdict?.payload, "approved");
    const mods = rows.filter((row) => row.kind === "modification").length;
    if (approved != null) return `${approved ? "Approved" : "Rejected"}${mods ? ` · ${mods} change${mods === 1 ? "" : "s"}` : " unchanged"}`;
  }
  if (seat === "smart_money" && rows.some((row) => row.kind === "finding")) {
    const count = rows.filter((row) => row.kind === "finding").length;
    return `${count} material finding${count === 1 ? "" : "s"} survived`;
  }
  if (direction !== "unknown") return `${seatNames[seat] || seat} · ${direction}`;
  return firstSentence(summary);
}
function changedRead(seat: string, currentRows: StoredResearchEvidence[], priorRows: StoredResearchEvidence[], currentCall: string | null, priorCall: string | null): string | null {
  if (seat === "news") {
    const changes = currentRows.flatMap((row) => objects(object(row.payload)?.state_changes));
    const latest = last(changes);
    if (latest) {
      const event = stringAt(latest, "event");
      const before = stringAt(latest, "previous_state"); const after = stringAt(latest, "new_state");
      const impact = stringAt(latest, "market_impact");
      const transition = before && after ? `${before} → ${after}` : after;
      const copy = [event, transition, impact].filter(Boolean).join(" · ");
      if (copy) return `News: ${editCopy(copy, 210)}`;
    }
  }
  if (!priorRows.length && !priorCall) return null;
  const currentDirection = aggregateDirection(currentRows); const priorDirection = aggregateDirection(priorRows);
  const currentSummary = rowSummary(representative(currentRows, seat)) || currentCall;
  const priorSummary = rowSummary(representative(priorRows, seat)) || priorCall;
  if (currentDirection !== "unknown" && priorDirection !== "unknown" && currentDirection !== priorDirection) {
    return `${seatNames[seat] || seat} moved ${priorDirection} → ${currentDirection}.${currentSummary ? ` ${editCopy(currentSummary, 180)}` : ""}`;
  }
  if (currentSummary && priorSummary && currentSummary.toLowerCase() !== priorSummary.toLowerCase()) {
    return `${seatNames[seat] || seat}: ${firstSentence(currentSummary, 165)}`;
  }
  return null;
}

function admissionDetail(row: StoredResearchEvidence | undefined): string | null {
  if (!row) return null;
  const value = numberAt(row.payload, "transaction_value_usd");
  return [
    stringAt(row.payload, "reason")?.replace(/_/g, " "), stringAt(row.payload, "sector"),
    value != null ? `$${value.toLocaleString("en-US")}` : null,
  ].filter(Boolean).join(" · ") || null;
}

function smartMoney(rows: StoredResearchEvidence[]): SmartMoneyFinding[] {
  const admissionRows = rows.filter((row) => seatOf(row.agent_name) === "smart_money" && row.kind === "admission" && row.state === "valid");
  const admissions = new Map(admissionRows.map((row) => [`${row.run_id}:${row.symbol}`, row]));
  const findingRows = rows.filter((row) => seatOf(row.agent_name) === "smart_money" && row.kind === "finding" && row.state === "valid");
  const findings = findingRows.map((row) => {
    const payload = object(row.payload) || {};
    const observations = objects(payload.observations);
    const classificationRaw = stringAt(payload, "economic_role", "classification", "actionability")?.toLowerCase();
    const freshnessRaw = observations.map((item) => stringAt(item, "freshness")).find(Boolean)?.toLowerCase()
      || stringAt(payload, "freshness")?.toLowerCase();
    const freshnessMap: Record<string, SmartMoneyFinding["freshness"]> = { fresh: "timely", real_time: "real_time", timely: "timely", delayed: "delayed", stale: "stale" };
    const event = last(observations.map((item) => stringAt(item, "transaction_date", "event_timestamp")).filter((value): value is string => Boolean(value)).sort()) || null;
    const knowable = last(observations.map((item) => stringAt(item, "known_at", "accepted_at", "disclosure_date")).filter((value): value is string => Boolean(value)).sort()) || row.timestamp;
    const lags = observations.map((item) => numberAt(item, "lag_days")).filter((value): value is number => value != null);
    const firstObservation = observations[0];
    const stream = stringAt(firstObservation, "stream") || stringAt(payload, "stream") || "disclosure";
    const admission = admissions.get(`${row.run_id}:${row.symbol}`);
    const admitted = Boolean(admission) || observations.some((item) => booleanAt(item, "transient_admitted") === true);
    const accessions = observations.map((item) => stringAt(item, "accession_number")).filter(Boolean);
    const actors = new Set(observations.map((item) => stringAt(item, "actor")).filter(Boolean));
    const sourceDetail = [
      accessions.length ? `${new Set(accessions).size} SEC accession${new Set(accessions).size === 1 ? "" : "s"}` : null,
      actors.size ? `${actors.size} independent owner${actors.size === 1 ? "" : "s"}` : null,
      observations.some((item) => booleanAt(item, "amendment") === true) ? "includes amendment" : null,
      observations.some((item) => booleanAt(item, "is_10b5_1") === true) ? "10b5-1 noted" : null,
    ].filter(Boolean).join(" · ") || null;
    const detail = admitted ? admissionDetail(admission) : null;
    const classification = (["actionable", "confirmatory", "contradictory", "historical"].includes(classificationRaw || "") ? classificationRaw : "historical") as SmartMoneyFinding["classification"];
    return {
      id: String(row.id), symbol: row.symbol, stream: stream === "insider" ? "SEC Form 4" : stream,
      headline: editCopy(stringAt(payload, "headline", "title"), 120) || `${row.symbol || "Market"} · ${classification} ${directionOf(payload)} evidence`,
      summary: rowSummary(row) || "A structured finding survived the deterministic noise filter; no narrative summary was stored.",
      classification, freshness: freshnessMap[freshnessRaw || ""] || "unknown",
      event_timestamp: event, knowable_timestamp: knowable,
      lag_days: lags.length ? Math.max(...lags) : numberAt(payload, "lag_days", "disclosure_lag_days"),
      materiality: editCopy(stringAt(payload, "why_now", "materiality", "why_it_matters")),
      source_name: stream === "insider" ? "SEC Form 4" : stringAt(firstObservation, "source_name", "provider") || "Stored source",
      source_url: stringAt(firstObservation, "source_url", "url") || stringAt(payload, "source_url", "url"),
      source_detail: sourceDetail, direction: directionOf(payload), observation_count: observations.length || 1,
      admitted_this_run: admitted, admission_detail: detail,
    };
  });
  const findingKeys = new Set(findingRows.map((row) => `${row.run_id}:${row.symbol}`));
  const admissionOnly = admissionRows.filter((row) => !findingKeys.has(`${row.run_id}:${row.symbol}`)).map((row): SmartMoneyFinding => {
    const payload = object(row.payload) || {};
    const rawAccessions = Array.isArray(payload.accessions) ? payload.accessions.filter((item) => typeof item === "string" && item) : [];
    const rawOwners = Array.isArray(payload.owners) ? payload.owners.filter((item) => typeof item === "string" && item) : [];
    const sourceDetail = [
      rawAccessions.length ? `${rawAccessions.length} SEC accession${rawAccessions.length === 1 ? "" : "s"}` : null,
      rawOwners.length ? `${rawOwners.length} independent owner${rawOwners.length === 1 ? "" : "s"}` : null,
    ].filter(Boolean).join(" · ") || null;
    return {
      id: `admission-${row.id}`, symbol: row.symbol, stream: "SEC Form 4",
      headline: `${row.symbol || "External symbol"} admitted for this run by deterministic SEC evidence`,
      summary: "A material open-market purchase passed the stored broker, price, history, liquidity and sector checks. Paid synthesis is not required for this admission fact to remain visible.",
      classification: "admission", freshness: "timely", event_timestamp: null,
      knowable_timestamp: row.timestamp, lag_days: null,
      materiality: "Run-scoped only. Permanent universe membership is unchanged.",
      source_name: "SEC Form 4", source_url: null, source_detail: sourceDetail,
      direction: "bullish", observation_count: 0, admitted_this_run: true,
      admission_detail: admissionDetail(row),
    };
  });
  return [...findings, ...admissionOnly];
}

function decisionSteps(run: ResearchRun | undefined): ResearchDecisionStep[] {
  if (!run) return [];
  const delta: ResearchDecisionDeltaRaw = run.decision_delta;
  const proposed = sortEvidence(delta.proposed);
  const risk = sortEvidence(delta.risk_changes);
  const events = sortEvidence(delta.deterministic_events);
  const verdict = risk.find((row) => row.kind === "verdict");
  const modifications = risk.filter((row) => row.kind === "modification");
  const runSeats = new Set([
    ...run.evidence.filter((row) => row.state === "valid" && SIGNAL_SEATS.has(seatOf(row.agent_name)) && !["provider_error", "analysis_error"].includes(row.kind)).map((row) => seatOf(row.agent_name)),
    ...run.agent_calls.filter((call) => SIGNAL_SEATS.has(seatOf(call.agent_name)) && (!call.status || ["success", "fallback"].includes(call.status))).map((call) => seatOf(call.agent_name)),
  ]);
  const specialistTimestamps = [
    ...run.evidence.filter((row) => SIGNAL_SEATS.has(seatOf(row.agent_name))).map((row) => row.timestamp),
    ...run.agent_calls.filter((call) => SIGNAL_SEATS.has(seatOf(call.agent_name))).map((call) => call.timestamp),
  ].filter((value): value is string => Boolean(value)).sort();
  const pmCall = last(run.agent_calls.filter((call) => seatOf(call.agent_name) === "portfolio_manager").sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp))));
  const riskCall = last(run.agent_calls.filter((call) => seatOf(call.agent_name) === "ai_risk").sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp))));
  const pmTargets = proposed.filter((row) => row.kind === "target");
  const pmOrders = proposed.filter((row) => row.kind === "proposed_order");
  const pmItems = (pmTargets.length ? pmTargets : pmOrders).slice(0, 3).map((row) => {
    const weight = numberAt(row.payload, "target_weight_pct", "allocation_pct");
    return `${row.symbol || stringAt(row.payload, "symbol") || "Portfolio"}${weight == null ? "" : ` ${weight}%`}`;
  });
  const count = pmTargets.length || pmOrders.length;
  const pmSummary = proposed.length ? `${count} ${pmTargets.length ? "target" : "order"}${count === 1 ? "" : "s"}: ${pmItems.join(" · ")}` : null;
  const approved = booleanAt(verdict?.payload, "approved");
  const riskSummary = verdict ? `${approved === false ? "Rejected" : "Approved"}${modifications.length ? ` with ${modifications.length} change${modifications.length === 1 ? "" : "s"}` : " unchanged"}.` : modifications.length ? `${modifications.length} risk change${modifications.length === 1 ? "" : "s"} recorded.` : null;
  const gates = events.filter((row) => stringAt(row.payload, "stage") === "deterministic_gate");
  const gateSummaries = [...new Set(gates.map((row) => [stringAt(row.payload, "outcome"), stringAt(row.payload, "reason")?.replace(/_/g, " ")].filter(Boolean).join(" · ")).filter(Boolean))];
  const gateSummary = gateSummaries.join("; ") || null;
  const modificationDetail = modifications.map((row) => {
    const field = stringAt(row.payload, "field")?.replace(/_/g, " ") || "value";
    const before = numberAt(row.payload, "original_value"); const after = numberAt(row.payload, "new_value");
    const deltaText = before != null && after != null ? ` ${before} → ${after}` : "";
    return `${row.symbol || stringAt(row.payload, "symbol") || "Book"}: ${field}${deltaText}${stringAt(row.payload, "reason") ? ` · ${stringAt(row.payload, "reason")}` : ""}`;
  }).join(" · ");
  const tradeSummaries = delta.trades.map((trade) => `${trade.action} ${trade.fill_qty ?? trade.qty ?? "—"} ${trade.symbol}${trade.fill_price != null ? ` @ ${trade.fill_price}` : ""}`);
  const tradeDetail = delta.trades.map((trade) => editCopy(trade.reasoning, 120)).filter(Boolean).join(" · ") || null;
  return [
    { stage: "read", status: "stored", summary: `${runSeats.size} specialist read${runSeats.size === 1 ? "" : "s"} ${runSeats.size === 1 ? "is" : "are"} stored for this run.`, detail: null, timestamp: last(specialistTimestamps) || null },
    { stage: "portfolio_manager", status: proposed.length ? "proposed" : "no proposal", summary: pmSummary || (pmCall?.status && !["success", "fallback"].includes(pmCall.status) ? failureHeadline("portfolio_manager") : "No PM proposal was stored for this run."), detail: rowSummary(representative(proposed, "portfolio_manager")), timestamp: last(proposed)?.timestamp || pmCall?.timestamp || null },
    { stage: "ai_risk", status: risk.length ? "reviewed" : "not reached", summary: riskSummary || (proposed.length ? riskCall?.status && !["success", "fallback"].includes(riskCall.status) ? failureHeadline("ai_risk") : "No AI Risk review was stored for this run." : "Not reached after PM produced no valid proposal."), detail: modificationDetail || rowSummary(verdict), timestamp: last(risk)?.timestamp || riskCall?.timestamp || null },
    { stage: "deterministic_gate", status: delta.state === "hard_risk_block" ? "blocked" : gates.length ? "recorded" : proposed.length ? "not recorded" : "not reached", summary: gateSummary || (proposed.length ? "No deterministic gate outcome was stored for this run." : "Not reached; no grounded PM proposal entered the gates."), detail: gates.length > 1 ? `${gates.length} deterministic gate events` : gates.length ? "deterministic gate" : null, timestamp: last(gates)?.timestamp || null },
    { stage: "execution", status: delta.trades.length ? "recorded" : "not executed", summary: delta.trades.length ? `${delta.trades.length} stored trade${delta.trades.length === 1 ? "" : "s"}: ${tradeSummaries.join("; ")}` : "No execution was stored for this run.", detail: tradeDetail, timestamp: last(delta.trades)?.timestamp || null },
  ];
}

export function buildResearchDesk(data: ResearchDailyResponse, priorDay: ResearchDailyResponse | null = null): ResearchDeskData {
  const allEvidence = data.runs.flatMap((run) => run.evidence);
  const agents: ResearchAgentBrief[] = SEATS.map((seat) => {
    const seatRuns = data.runs.map((run) => ({
      rows: sortEvidence(run.evidence.filter((row) => seatOf(row.agent_name) === seat && row.state === "valid" && !["agent_failure", "provider_error", "analysis_error"].includes(row.kind))),
      calls: [...run.agent_calls].filter((call) => seatOf(call.agent_name) === seat).sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp))),
    })).filter((run) => run.rows.length || run.calls.length);
    const priorDayRuns = (priorDay?.runs || []).map((run) => ({
      rows: sortEvidence(run.evidence.filter((row) => seatOf(row.agent_name) === seat && row.state === "valid" && !["agent_failure", "provider_error", "analysis_error"].includes(row.kind))),
      calls: [...run.agent_calls].filter((call) => seatOf(call.agent_name) === seat).sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp))),
    })).filter((run) => run.rows.length || run.calls.length);
    const current = last(seatRuns);
    const previous = seatRuns.length > 1 ? seatRuns[seatRuns.length - 2] : last(priorDayRuns);
    const currentRows = current?.rows || []; const rep = representative(currentRows, seat);
    const call = current ? last(current.calls) : undefined; const priorCall = previous ? last(previous.calls) : undefined;
    const callFailed = Boolean(call?.status && !["success", "fallback", "hard_risk_block"].includes(call.status));
    const callRead = callFailed ? null : editCopy(call?.output_summary);
    const structuredRead = rowSummary(rep);
    const read = structuredRead || callRead;
    const headline = !currentRows.length && callFailed ? failureHeadline(seat) : structuredHeadline(seat, currentRows, read);
    const allRowsForSeat = allEvidence.filter((row) => seatOf(row.agent_name) === seat);
    const failedCall = callFailed ? call : undefined;
    const missingSeat = data.missing_sources.some((source) => seatOf(source.split("/")[0]) === seat);
    const freshnessStatus: ResearchItemStatus = data.freshness.label === "aging" ? "aging" : data.freshness.label === "stale" ? "stale" : data.freshness.label === "historical" ? "historical" : "current";
    const seatStatus: ResearchItemStatus = failedCall || missingSeat || allRowsForSeat.some((row) => row.state === "invalid")
      ? "error" : current ? freshnessStatus : "unavailable";
    const newsChange = seat === "news" ? last(currentRows.flatMap((row) => objects(object(row.payload)?.state_changes))) : undefined;
    return {
      seat, status: seatStatus,
      headline, read: headline && read && headline.toLowerCase() === read.toLowerCase() ? null : read,
      direction: aggregateDirection(currentRows), evidence: evidenceChips(currentRows),
      changed: changedRead(seat, currentRows, previous?.rows || [], callRead, editCopy(priorCall?.output_summary)),
      tension: null, why_now: editCopy(stringAt(rep?.payload, "why_now", "catalyst", "shift_reason") || stringAt(newsChange, "market_impact", "event"), 220),
      market_context: seat === "technical" ? marketContext(currentRows) : [],
      timestamp: call?.timestamp || rep?.timestamp || null,
      error: failedCall ? "Agent call failed; no conclusion is inferred." : allRowsForSeat.some((row) => row.state === "invalid") ? "Some structured evidence was invalid." : null,
    };
  });

  const directional = agents.filter((agent) => SIGNAL_SEATS.has(agent.seat) && ["bullish", "bearish"].includes(agent.direction));
  const bullish = directional.filter((agent) => agent.direction === "bullish");
  const bearish = directional.filter((agent) => agent.direction === "bearish");
  const inferredTension = bullish.length && bearish.length
    ? `${bullish.map((agent) => seatNames[agent.seat]).join(", ")} read bullish; ${bearish.map((agent) => seatNames[agent.seat]).join(", ")} read bearish.` : null;
  const explicitTensionRaw = stringAt(
    representative(allEvidence.filter((row) => seatOf(row.agent_name) === "portfolio_manager" && row.state === "valid"), "portfolio_manager")?.payload,
    "signal_conflicts", "tension", "contradiction",
  );
  const explicitTension = explicitTensionRaw && !/(?:\b(?:n\/a|unavailable)\b|\w+=)/i.test(explicitTensionRaw) ? editCopy(explicitTensionRaw, 240) : null;
  const tension = explicitTension || inferredTension;
  for (const agent of agents) {
    const opposition = agent.direction === "bullish" ? bearish : agent.direction === "bearish" ? bullish : [];
    if (opposition.length) agent.tension = `Conflicts with ${opposition.map((item) => seatNames[item.seat]).join(", ")}.`;
  }
  const signals = agents.filter((agent) => SIGNAL_SEATS.has(agent.seat) && agent.status !== "unavailable").map((agent) => ({
    seat: agent.seat, direction: agent.direction, signal: agent.headline || agent.read,
    relationship: agent.direction === "unknown" || agent.direction === "neutral" ? "independent" as const
      : agent.direction === "mixed" || (agent.direction === "bullish" ? bearish.length : bullish.length) ? "conflicts" as const : "agrees" as const,
    timestamp: agent.timestamp,
  }));
  const decisionRun = [...data.runs].reverse().find((run) => {
    const item = run.decision_delta;
    return Boolean(item.decision_id || item.proposed.length || item.risk_changes.length || item.trades.length || item.state === "hard_risk_block"
      || run.agent_calls.some((call) => ["portfolio_manager", "ai_risk"].includes(seatOf(call.agent_name))));
  });
  const delta = decisionRun?.decision_delta;
  const findings = smartMoney(allEvidence);
  const whatChanged = agents.map((agent) => agent.changed).filter((item): item is string => Boolean(item)).slice(0, 4);
  const pm = agents.find((agent) => agent.seat === "portfolio_manager");
  const pmStructured = rowSummary(representative(allEvidence.filter((row) => seatOf(row.agent_name) === "portfolio_manager" && row.kind === "reasoning" && row.state === "valid"), "portfolio_manager"));
  const directionalSummary = signals.filter((signal) => signal.direction !== "unknown").map((signal) => `${seatNames[signal.seat]} ${signal.direction}`).join(" · ");
  const fallbackRead = agents.find((agent) => ["macro", "news", "technical"].includes(agent.seat) && (agent.read || agent.headline));
  const thesis = pm && pm.status !== "error" && pm.status !== "unavailable" ? pmStructured || pm.read || pm.headline
    : directionalSummary ? `Specialist read: ${directionalSummary}.` : fallbackRead?.read || fallbackRead?.headline || null;
  const changedAgent = agents.find((agent) => agent.changed);
  const topWhyNow = agents.find((agent) => ["smart_money", "news", "earnings", "technical", "macro"].includes(agent.seat) && agent.why_now)?.why_now
    || findings.find((item) => item.materiality)?.materiality
    || (changedAgent ? `The latest ${seatNames[changedAgent.seat]} read differs from the prior useful read.` : null);
  let dryAnnotation: string | null = null;
  if (data.state === "partial") dryAnnotation = "One or more seats are missing. The surviving evidence keeps its original weight.";
  else if (delta?.state === "hard_risk_block") dryAnnotation = "The thesis reached the gate. The order did not.";
  else if (delta?.state === "no_proposal" && signals.some((signal) => signal.direction !== "unknown")) dryAnnotation = "Several reads. No portfolio instruction. That is still a decision.";
  else if (findings.some((item) => item.classification === "historical")) dryAnnotation = "Interesting is not the same as tradable. The timestamps do the editing.";
  else if (tension && delta?.trades.length) dryAnnotation = "The disagreement survived. So did an order.";

  const usefulRuns = data.runs.filter((run) => run.evidence.some((row) => row.state === "valid" && SIGNAL_SEATS.has(seatOf(row.agent_name)) && !["scan_summary", "admission", "provider_error", "analysis_error"].includes(row.kind)));
  const priorAsOf = usefulRuns.length > 1 ? usefulRuns[usefulRuns.length - 2]?.summary.last_timestamp || null
    : priorDay?.freshness.latest_recorded_at || null;
  return {
    date: data.date, status: statusFor(data), as_of: data.freshness.latest_recorded_at || (data.state === "empty" ? null : data.as_of), prior_as_of: priorAsOf,
    thesis, what_changed: whatChanged, tension, why_now: topWhyNow, dry_annotation: dryAnnotation,
    agents, signal_stack: signals, decision_run_id: decisionRun?.summary.run_id || null, decision_chain: decisionSteps(decisionRun), smart_money: findings,
    reviews: {
      daily_result: dailyResult(data),
      position_reviewer: agents.find((agent) => agent.seat === "position_reviewer")?.read || agents.find((agent) => agent.seat === "position_reviewer")?.headline || null,
      evening_review: agents.find((agent) => agent.seat === "evening_review")?.read || agents.find((agent) => agent.seat === "evening_review")?.headline || null,
      meta_reflection: agents.find((agent) => agent.seat === "meta_reflection")?.read || agents.find((agent) => agent.seat === "meta_reflection")?.headline || null,
      lesson_learned: editCopy(data.reflection?.lessons),
      suggested_actions: storedList(data.reflection?.suggested_actions),
      tomorrow_watch: [editCopy(data.reflection?.tomorrow_outlook), ...storedList(data.reflection?.tomorrow_key_risks)].filter((item): item is string => Boolean(item)),
    },
    errors: [data.read_error, ...data.missing_sources.map((source) => `Missing source: ${source}`)].filter((item): item is string => Boolean(item)),
  };
}
