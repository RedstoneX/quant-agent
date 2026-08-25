import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const baseUrl = process.env.QAMC_VISUAL_URL || "http://127.0.0.1:5173/cockpit/";
const output = resolve("../docs/verification/dashboard-finish-line");
const researchOutput = resolve("../docs/verification/research-intelligence-desk");
await mkdir(output, { recursive: true });
await mkdir(researchOutput, { recursive: true });

const runId = "morning-20260825-demo";
const events = [
  ["opportunity", "discovered", "actionable_technical_prefilter"],
  ["specialist", "evaluated", "technical_analysis_validated"],
  ["portfolio_manager", "proposed", "constructor_created_order"],
  ["risk", "modified", "risk_manager_verdict"],
  ["deterministic_gate", "allowed", "post_risk_checks_passed"],
  ["funding", "resized", "confirmed_cash_partially_funded_order"],
  ["order", "submitted", "broker_accepted"],
  ["order", "filled", "broker_terminal_status"],
  ["protection", "placed", "protective_stop_result"],
  ["position_management", "exited", "confirmed_exit_fill"],
].map(([stage, outcome, reason], index) => ({
  stage, outcome, reason, timestamp: `2026-08-25T14:${String(index * 3).padStart(2, "0")}:00Z`,
  details: stage === "protection" ? { stop_price: 218.4, entry_order_id: "alpaca-1" } : {},
}));

const trade = {
  id: 11, symbol: "AAPL", action: "BUY", qty: 12, price: 221.4,
  reasoning: "Grounded technical continuation with risk-sized exposure.", run_id: runId,
  decision_id: "decision-1", broker_order_id: "alpaca-1", fill_status: "filled",
  fill_qty: 12, fill_price: 221.45, realized_pnl: null, fill_reconciled_at: "2026-08-25T14:24:00Z",
  timestamp: "2026-08-25T14:18:00Z", stop_loss: 218.4, take_profit: 232.0,
};
const exitTrade = { ...trade, id: 12, action: "SELL", broker_order_id: "alpaca-2", price: 226.2, fill_price: 226.2, realized_pnl: 57, timestamp: "2026-08-25T18:20:00Z" };
const candidate = {
  symbol: "AAPL", direction: "bullish", is_bearish_hedge: false, reached_pm_target: true,
  pm_target_weight_pct: 8, reached_proposed_order: true, proposed_action: "BUY", risk_modified: true,
  executed: true, trade_action: "BUY", order_status: "filled", fill_qty: 12, fill_price: 221.45,
  realized_pnl: 57, protection_outcome: "placed", pipeline_events: events,
  execution_skip_reason: null, execution_skip_detail: null,
};
const funnel = {
  run_id: runId, session_prefix: "morning", timestamp: "2026-08-25T14:00:00Z",
  candidates: [candidate, { ...candidate, symbol: "MSFT", direction: "neutral", reached_pm_target: false, pm_target_weight_pct: null, reached_proposed_order: false, proposed_action: null, risk_modified: false, executed: false, trade_action: null, order_status: null, fill_qty: null, fill_price: null, realized_pnl: null, protection_outcome: null, pipeline_events: events.slice(0, 3) }],
  candidates_considered: 2, reached_pm_count: 1, proposed_order_count: 1, executed_count: 1,
  bearish_hedge_considered: true, hard_risk_block: false, pipeline_events: events,
  pm_reasoning: { portfolio_view: "Selective long exposure with cash reserve maintained.", reasoning_chain: { macro_filter: "Constructive but selective.", signal_conflicts: "AAPL technical buy; news neutral." }, timestamp: "2026-08-25T14:06:00Z" },
  risk_verdict: { verdict: { approved: true, reasoning: "Approved after size reduction.", reasoning_chain: { rr_audit: "Adequate", sizing_sanity: "Reduced" }, reason_category: "oversized", modifications: [{ symbol: "AAPL", field: "allocation_pct", original_value: 10, new_value: 8, reason: "Concentration" }], scale_all_buys: 1 }, timestamp: "2026-08-25T14:09:00Z" },
  macro_context: { regime: "selective risk-on", equity_outlook: "bullish", confidence: "medium", summary: "Participation is narrow; preserve liquidity.", sector_guidance: [], timestamp: "2026-08-25T13:55:00Z" },
  decision_state: "executed",
};
const detail = {
  run_id: runId, symbol: "AAPL", decision_id: "decision-1",
  tech: { symbol: "AAPL", rating: "buy", conviction: "high", entry_price: 221, reference_target: 232, stop_loss: 219.8, reasoning_chain: { trend: "Above rising averages", momentum: "Positive" }, reasoning: "Trend and volume support continuation.", thesis_invalid_if: "Daily close below support", signal_age_days: 0 },
  earnings: null,
  news_symbol: [{ headline: "Product demand remains resilient", sentiment: "neutral", conviction: "medium", impact_summary: "No near-term thesis break." }],
  macro_context: funnel.macro_context,
  news_context: { market_sentiment: "neutral", confidence: "medium", pm_briefing: "Avoid extrapolating headlines.", era_themes: ["AI capex"], current_regime: "selective risk-on", relevant_state_changes: [], timestamp: "2026-08-25T13:54:00Z" },
  pm_reasoning: funnel.pm_reasoning,
  pm_target: { symbol: "AAPL", target_weight_pct: 8, conviction: "medium", thesis: "Technical setup supports a bounded long.", thesis_invalid_if: "Break below support", suggested_stop_price: 219.8, catalyst: "Continuation" },
  pm_proposed_order: { action: "BUY", symbol: "AAPL", allocation_pct: 8, entry_price: 221.4, stop_loss: 219.8, take_profit: 232, reasoning: "Grounded target converted to order." },
  risk_verdict: funnel.risk_verdict, risk_modification: { symbol: "AAPL", field: "allocation_pct", original_value: 10, new_value: 8, reason: "Concentration" },
  trade, trades: [trade, exitTrade], pipeline_events: events,
  consensus: { signals: [{ source: "tech_analyst", direction: "bullish", detail: "buy" }, { source: "news_analyst", direction: "neutral", detail: "neutral" }], agreement: "aligned" },
};
const account = {
  cash: 6200, portfolio_value: 100000, last_equity: 99400, daily_pnl: 600, daily_pnl_pct: 0.6, paper: true,
  history: Array.from({ length: 10 }, (_, i) => ({ date: `2026-08-${String(15 + i).padStart(2, "0")}`, total_value: 98000 + i * 220, daily_pnl: 220, daily_return_pct: 0.22, equity_close: 98000 + i * 220 })),
  liquidity: { sweep_enabled: true, sweep_symbol: "SGOV", raw_cash: 6200, sweep_parked_value: 26000, reserve_usd: 5000, deployable_cash: 1200, total_liquidity: 32200 },
  risk_limits: { max_position_pct: 15, max_total_position_pct: 75, max_daily_loss_pct: 2, max_sector_pct: 30 }, error: null,
};
const positions = [
  { symbol: "AAPL", qty: 12, avg_entry: 221.45, current_price: 226.2, market_value: 2714.4, unrealized_pnl: 57, unrealized_intraday_pnl: 34, sector: "Technology", is_cash_equivalent: false, direction: "long" },
  { symbol: "SQQQ", qty: 40, avg_entry: 31.2, current_price: 30.8, market_value: 1232, unrealized_pnl: -16, unrealized_intraday_pnl: -9, sector: "Inverse ETF", is_cash_equivalent: false, direction: "bearish_hedge" },
  { symbol: "SGOV", qty: 259, avg_entry: 100.2, current_price: 100.39, market_value: 26001, unrealized_pnl: 49, unrealized_intraday_pnl: 3, sector: "Cash equivalent", is_cash_equivalent: true, direction: "cash_equivalent" },
];
const orders = [
  { id: "alpaca-1", symbol: "AAPL", side: "buy", qty: 12, order_type: "limit", status: "filled", limit_price: 221.5, stop_price: null, filled_qty: 12, filled_avg_price: 221.45, submitted_at: "2026-08-25T14:18:00Z", filled_at: "2026-08-25T14:20:00Z" },
  { id: "stop-1", symbol: "AAPL", side: "sell", qty: 12, order_type: "stop", status: "accepted", limit_price: null, stop_price: 218.4, filled_qty: 0, filled_avg_price: null, submitted_at: "2026-08-25T14:21:00Z", filled_at: null },
];
const bars = Array.from({ length: 60 }, (_, i) => {
  const timestamp = new Date(Date.UTC(2026, 5, 1 + i)).toISOString();
  return { date: timestamp.slice(0, 10), timestamp, open: 205 + i * .3, high: 207 + i * .3, low: 203 + i * .3, close: 206 + i * .32, volume: 50000000 + i * 100000 };
});
const runSummary = { run_id: runId, session_prefix: "morning", first_timestamp: "2026-08-25T14:00:00Z", last_timestamp: "2026-08-25T14:25:00Z", agent_count: 8, decision_id: "decision-1", total_cost_usd: 0.42 };
const priorRunId = "morning-20260824-demo";
const priorEvidence = [
  { id: 101, run_id: priorRunId, decision_id: null, agent_name: "tech_analyst", kind: "analysis", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-24T13:03:00Z", state: "valid", payload: { rating: "neutral", conviction: "medium", reasoning: "Support had not confirmed." } },
  { id: 102, run_id: priorRunId, decision_id: null, agent_name: "macro_analyst", kind: "analysis", scope: "run", symbol: null, timestamp: "2026-08-24T12:55:00Z", state: "valid", payload: { regime: "transitional", equity_outlook: "bullish", confidence: "medium", summary: "Liquidity was constructive, with breadth still unresolved." } },
];
const researchEvidence = [
  { id: 1, run_id: runId, decision_id: "decision-1", agent_name: "tech_analyst", kind: "analysis", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:03:00Z", state: "valid", payload: { rating: "buy", conviction: "high", entry_price: 221, stop_loss: 218.4, reference_target: 232, reasoning: "Price holds above rising averages with constructive volume." } },
  { id: 2, run_id: runId, decision_id: "decision-1", agent_name: "news_analyst", kind: "analysis", scope: "run", symbol: null, timestamp: "2026-08-25T14:04:00Z", state: "valid", payload: { market_sentiment: "neutral", confidence: "medium", pm_briefing: "No fresh headline breaks the AAPL thesis; avoid promoting quiet news into a catalyst." } },
  { id: 3, run_id: runId, decision_id: "decision-1", agent_name: "macro_analyst", kind: "analysis", scope: "run", symbol: null, timestamp: "2026-08-25T14:05:00Z", state: "valid", payload: { regime: "transitional", equity_outlook: "bearish", confidence: "medium", shift_reason: "Breadth deteriorated after the earlier read.", summary: "Liquidity remains usable, but weaker breadth argues for smaller exposure." } },
  { id: 4, run_id: runId, decision_id: "decision-1", agent_name: "earnings_analyst", kind: "analysis", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:06:00Z", state: "valid", payload: { filing_date: "2026-08-01", investment_implications: { sentiment: "neutral", conviction: "medium", key_thesis: "Services margins remain supportive; no new filing changes the read." } } },
  { id: 5, run_id: runId, decision_id: "decision-1", agent_name: "smart_money_analyst", kind: "scan_summary", scope: "run", symbol: null, timestamp: "2026-08-25T14:07:00Z", state: "valid", payload: { source: "SEC Form 4", observations: 2, findings: 1, temporary_admissions: ["SPIR"], state: "material" } },
  { id: 6, run_id: runId, decision_id: "decision-1", agent_name: "smart_money_analyst", kind: "admission", scope: "symbol", symbol: "SPIR", timestamp: "2026-08-25T14:07:01Z", state: "valid", payload: { temporary: true, reason: "material_sec_form4_purchase", accessions: ["0000000001-26-000001"], owners: ["Example Insider"], transaction_value_usd: 251250, last_price: 14.22, avg_dollar_volume_20d_usd: 12400000, sector: "Industrials", broker: { eligible: true, exchange: "NASDAQ" } } },
  { id: 7, run_id: runId, decision_id: "decision-1", agent_name: "smart_money_analyst", kind: "finding", scope: "symbol", symbol: "SPIR", timestamp: "2026-08-25T14:07:02Z", state: "valid", payload: { stance: "bullish", economic_role: "actionable", summary: "A fresh open-market purchase cleared the materiality filter.", why_now: "The Form 4 was accepted yesterday and remains fresh.", support_eligible: true, transient_admission_eligible: true, observations: [{ symbol: "SPIR", stream: "insider", actor: "Example Insider", actor_roles: ["director"], direction: "buy", transaction_date: "2026-08-22", disclosure_date: "2026-08-24", accepted_at: "2026-08-24T21:30:15Z", known_at: "2026-08-24T21:30:15Z", source_url: "https://www.sec.gov/Archives/edgar/data/0001/000000000126000001/filing.txt", accession_number: "0000000001-26-000001", filing_form: "4", transaction_code: "P", shares: 2500, price_per_share: 100.5, transaction_value_usd: 251250, amendment: false, is_10b5_1: false, transient_admitted: true, lag_days: 2, disclosure_age_days: 1, freshness: "fresh", economic_role: "actionable" }] } },
  { id: 8, run_id: runId, decision_id: "decision-1", agent_name: "portfolio_manager", kind: "reasoning", scope: "run", symbol: null, timestamp: "2026-08-25T14:08:00Z", state: "valid", payload: { portfolio_view: "Selective AAPL long; keep the cash reserve intact.", reasoning_chain: { signal_conflicts: "Technical and fresh Form 4 evidence are constructive; Macro remains bearish." } } },
  { id: 9, run_id: runId, decision_id: "decision-1", agent_name: "portfolio_manager", kind: "target", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:09:00Z", state: "valid", payload: { symbol: "AAPL", target_weight_pct: 8, conviction: "medium", thesis: "Technical setup supports a bounded long.", thesis_invalid_if: "Break below support" } },
  { id: 10, run_id: runId, decision_id: "decision-1", agent_name: "risk_manager", kind: "verdict", scope: "run", symbol: null, timestamp: "2026-08-25T14:10:00Z", state: "valid", payload: { approved: true, reasoning: "Approved after reducing concentration.", reason_category: "concentration", modifications: [{ symbol: "AAPL", field: "allocation_pct", original_value: 10, new_value: 8, reason: "Concentration" }] } },
  { id: 11, run_id: runId, decision_id: "decision-1", agent_name: "risk_manager", kind: "modification", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:10:01Z", state: "valid", payload: { symbol: "AAPL", field: "allocation_pct", original_value: 10, new_value: 8, reason: "Concentration" } },
  { id: 12, run_id: runId, decision_id: "decision-1", agent_name: "pipeline", kind: "pipeline_event", scope: "symbol", symbol: "AAPL", timestamp: "2026-08-25T14:11:00Z", state: "valid", payload: { stage: "deterministic_gate", outcome: "allowed", reason: "post_risk_checks_passed" } },
];
const researchCalls = [
  ["tech_analyst", "Trend remains constructive, but the setup fails below support."], ["news_analyst", "No fresh headline changes the thesis."],
  ["macro_analyst", "Narrow breadth keeps sizing conservative."], ["earnings_analyst", "No new filing changes the earnings read."],
  ["smart_money_analyst", "Timely insider activity confirms the setup; it does not create it."], ["portfolio_manager", "Selective AAPL long with cash reserve maintained."],
  ["risk_manager", "Approved after reducing concentration from ten to eight percent."], ["position_reviewer", "Protection held and the exit respected the plan."],
  ["evening_analyst", "Execution was clean; patience added more than prediction."], ["meta_reflector", "Mean-reversion prompts need an explicit trend veto."],
].map(([agent_name, output_summary], id) => ({ id: id + 1, agent_name, run_id: runId, decision_id: "decision-1", timestamp: `2026-08-25T${String(14 + Math.min(id, 6)).padStart(2, "0")}:00:00Z`, status: "success", output_summary, requested_provider: "openrouter", requested_model: "model", actual_provider: "openrouter", model: "model", prompt_version: "v1", latency_s: 1.2, cost_usd: .01, structured_evidence_count: researchEvidence.filter((e) => e.agent_name === agent_name).length }));
const research = { date: "2026-08-25", as_of: "2026-08-25T20:05:00Z", state: "complete", freshness: { latest_recorded_at: "2026-08-25T20:05:00Z", age_minutes: 3, label: "current" }, read_error: null, missing_sources: [], daily_pnl: account.history.at(-1), reflection: { date: "2026-08-25", tomorrow_outlook: "Watch support before adding exposure.", lessons: "Patience added more than prediction.", suggested_actions: "[\"Keep size below the concentration cap.\"]", risk_rating: "medium", tomorrow_bias: "neutral", tomorrow_conviction: "medium", tomorrow_key_risks: "[\"Breadth\",\"Concentration\"]", sell_decisions_assessment: null, sell_grades_json: null, buy_grades_json: null, missed_opportunities_json: null, timestamp: "2026-08-25T20:00:00Z" }, runs: [
  { summary: runSummary, agent_calls: researchCalls, evidence: researchEvidence, decision_delta: { run_id: runId, decision_id: "decision-1", state: "executed", proposed: researchEvidence.filter((item) => item.agent_name === "portfolio_manager" && item.kind === "target"), risk_changes: researchEvidence.filter((item) => item.agent_name === "risk_manager"), deterministic_events: researchEvidence.filter((item) => item.kind === "pipeline_event"), trades: [trade] } },
] };
const priorResearch = { ...research, date: "2026-08-24", as_of: "2026-08-24T13:03:00Z", freshness: { latest_recorded_at: "2026-08-24T13:03:00Z", age_minutes: null, label: "historical" }, daily_pnl: null, reflection: null, runs: [{ summary: { ...runSummary, run_id: priorRunId, first_timestamp: "2026-08-24T12:55:00Z", last_timestamp: "2026-08-24T13:03:00Z", decision_id: null, agent_count: 2 }, agent_calls: [], evidence: priorEvidence, decision_delta: { run_id: priorRunId, decision_id: null, state: "no_proposal", proposed: [], risk_changes: [], deterministic_events: [], trades: [] } }] };

function json(route, body, status = 200) { return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }); }

async function installRoutes(page, scenario = "populated") {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/account") return json(route, scenario === "error" ? { ...account, error: "broker snapshot unavailable" } : account);
    if (path === "/positions") return json(route, { positions: scenario === "empty" || scenario === "error" ? [] : positions, error: scenario === "error" ? "position snapshot unavailable" : null });
    if (path === "/orders") return json(route, { orders: scenario === "empty" || scenario === "error" ? [] : orders, error: scenario === "error" ? "order snapshot unavailable" : null });
    if (path === "/trades") return json(route, { trades: scenario === "empty" || scenario === "error" ? [] : [exitTrade, trade], count: scenario === "empty" || scenario === "error" ? 0 : 2 });
    if (path === "/health") return json(route, { status: "healthy", db_reachable: true, broker_reachable: true, paper: true, sessions_logged_today: [runId], last_run_files: {}, session_lock_active: false, timestamp: "2026-08-25T18:30:00Z" });
    if (path === "/runs") return json(route, { runs: scenario === "empty" ? [] : [runSummary] });
    if (path === `/runs/${runId}/funnel`) return json(route, funnel);
    if (path === `/runs/${runId}`) return json(route, { run_id: runId, agent_logs: [], trades: [trade, exitTrade], decision_id: "decision-1", total_cost_usd: .42, hard_risk_block_recorded: false });
    if (path === `/runs/${runId}/candidates`) return json(route, { run_id: runId, candidates: ["AAPL", "MSFT"] });
    if (path === `/runs/${runId}/candidates/AAPL`) return json(route, detail);
    if (path === `/runs/${runId}/candidates/MSFT`) return json(route, { ...detail, symbol: "MSFT", tech: null, news_symbol: [], pm_target: null, pm_proposed_order: null, trade: null, trades: [], pipeline_events: events.slice(0, 3) });
    if (path === "/journal/dates") return json(route, { dates: ["2026-08-25"] });
    if (path.startsWith("/research/daily/")) {
      const requestedDate = path.split("/").at(-1);
      if (requestedDate !== research.date) return json(route, { ...priorResearch, date: requestedDate });
      if (scenario === "empty") return json(route, { ...research, as_of: null, state: "empty", freshness: { latest_recorded_at: null, age_minutes: null, label: "unknown" }, runs: [], reflection: null, daily_pnl: null });
      if (scenario === "error") return json(route, { ...research, state: "partial", read_error: "smart-money provider unavailable", missing_sources: ["smart_money"], runs: research.runs.map((run) => ({ ...run, evidence: run.evidence.filter((item) => item.agent_name !== "smart_money_analyst"), agent_calls: run.agent_calls.filter((item) => item.agent_name !== "smart_money_analyst") })) });
      if (scenario === "stale") return json(route, { ...research, freshness: { latest_recorded_at: "2026-08-25T14:11:00Z", age_minutes: 500, label: "stale" } });
      return json(route, research);
    }
    if (path.startsWith("/journal/")) {
      if (scenario === "empty" || scenario === "error") return json(route, { date: "2026-08-25", has_data: false, daily_pnl: null, reflection: null, runs: [], trades: [], candidates: [] });
      return json(route, { date: "2026-08-25", has_data: true, daily_pnl: account.history.at(-1), reflection: { date: "2026-08-25", tomorrow_outlook: "Selective", lessons: "Respect grounded passes.", suggested_actions: null, risk_rating: "medium", tomorrow_bias: "neutral", tomorrow_conviction: "medium", tomorrow_key_risks: "Concentration", sell_decisions_assessment: null, sell_grades_json: null, buy_grades_json: null, missed_opportunities_json: JSON.stringify([{ symbol: "NVDA", move_pct: 4.2, miss_category: "late_signal", lesson: "Wait for confirmed entry." }, { symbol: "TSLA", move_pct: -3.1, miss_category: "risk_disciplined", lesson: "Pass was correct." }]), timestamp: "2026-08-25T20:00:00Z" }, runs: [runSummary], trades: [trade, exitTrade], candidates: ["AAPL", "MSFT"] });
    }
    if (path.startsWith("/prices/")) return json(route, { symbol: decodeURIComponent(path.split("/").at(-1)), timeframe: url.searchParams.get("timeframe") || "1d", bars, error: null });
    if (path === "/quotes") return json(route, { quotes: [{ symbol: "AAPL", last_price: 226.2, prev_close: 224.1, session_open: 224.5, session_high: 227.0, session_low: 223.8 }, { symbol: "SPY", last_price: 655, prev_close: 652, session_open: 653, session_high: 656, session_low: 651 }], as_of: "2026-08-25T18:30:00Z", source: "alpaca_market_data", error: null });
    if (path === "/search") return json(route, { query: "", trades: [], agent_logs: [] });
    return route.continue();
  });
}

const browser = await chromium.launch({ headless: true });
const browserErrors = [];

async function shot(name, viewport, scenario = "populated", interact) {
  const context = await browser.newContext({ viewport, colorScheme: "dark" });
  const page = await context.newPage();
  page.on("console", (message) => { if (message.type() === "error") { const text = `${name}: console: ${message.text()}`; browserErrors.push(text); console.error(text); } });
  page.on("pageerror", (error) => { const text = `${name}: pageerror: ${error.message}`; browserErrors.push(text); console.error(text); });
  await installRoutes(page, scenario);
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByText("QAMC Mission Control", { exact: false }).first().waitFor();
  if (interact) await interact(page);
  await page.waitForTimeout(400);
  const overflow = await page.evaluate(() => {
    if (document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1) return null;
    const offenders = [...document.querySelectorAll("body *")]
      .filter((element) => element.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
      .slice(0, 5)
      .map((element) => `${element.tagName.toLowerCase()}.${element.className}`);
    return offenders.join(" | ");
  });
  if (overflow) browserErrors.push(`${name}: document has horizontal overflow (${overflow})`);
  await page.screenshot({ path: resolve(Number(name.slice(0, 2)) >= 8 ? researchOutput : output, `${name}.png`), fullPage: true });
  await context.close();
}

await shot("01-desktop-cockpit-populated", { width: 1600, height: 1000 });
await shot("02-desktop-positions-liquidity", { width: 1600, height: 1000 }, "populated", async (page) => {
  await page.getByText("Positions & Liquidity", { exact: true }).click();
});
await shot("03-desktop-candidate-lifecycle", { width: 1600, height: 1000 }, "populated", async (page) => {
  await page.getByRole("button", { name: /Lifecycle/ }).click();
  const lifecycle = page.getByText("Persisted lifecycle", { exact: false });
  await lifecycle.waitFor();
  await lifecycle.scrollIntoViewIfNeeded();
});
await shot("04-ipad-landscape-chart", { width: 1180, height: 820 }, "populated", async (page) => {
  await page.getByRole("button", { name: "Chart", exact: true }).click();
});
await shot("05-ipad-portrait-candidates", { width: 820, height: 1180 });
await shot("06-desktop-no-session-empty", { width: 1600, height: 1000 }, "empty");
await shot("07-ipad-portrait-read-errors", { width: 820, height: 1180 }, "error");
await shot("08-desktop-research-desk", { width: 1600, height: 1000 }, "populated", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByText("Research Intelligence Desk").waitFor(); await page.getByText(/Technical moved neutral → bullish/).first().waitFor(); await page.getByText("Breadth deteriorated after the earlier read.").first().waitFor(); await page.getByText("The disagreement survived. So did an order.").waitFor(); });
await shot("09-ipad-portrait-research-brief", { width: 820, height: 1180 }, "populated", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByText("Research Intelligence Desk").waitFor(); });
await shot("10-ipad-landscape-research-decision", { width: 1180, height: 820 }, "populated", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByRole("button", { name: "decision", exact: true }).click(); });
await shot("11-desktop-research-partial", { width: 1600, height: 1000 }, "error", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByText("Missing source: smart_money").waitFor(); });
await shot("12-desktop-research-smart-money", { width: 1600, height: 1000 }, "populated", async (page) => {
  await page.getByRole("button", { name: "Research Desk" }).click();
  await page.getByText("Smart Money", { exact: true }).last().click();
  await page.getByText("admitted this run", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Maximize active panel" }).click();
  await page.getByRole("button", { name: "Restore workspace" }).waitFor();
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Research Desk" }).click();
  await page.getByRole("button", { name: "Restore workspace" }).waitFor();
  await page.getByText("SEC Form 4", { exact: false }).first().waitFor();
});
await shot("13-ipad-portrait-research-signals", { width: 820, height: 1180 }, "populated", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByRole("button", { name: "signals", exact: true }).click(); await page.getByText("admitted this run", { exact: true }).waitFor(); });
await shot("14-ipad-landscape-research-review", { width: 1180, height: 820 }, "populated", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByRole("button", { name: "review", exact: true }).click(); await page.getByText("Mean-reversion prompts need an explicit trend veto.").waitFor(); });
await shot("15-ipad-portrait-research-empty", { width: 820, height: 1180 }, "empty", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByText("Quiet is a valid read.").waitFor(); });
await shot("16-desktop-research-stale", { width: 1600, height: 1000 }, "stale", async (page) => { await page.getByRole("button", { name: "Research Desk" }).click(); await page.getByText("Last-known research is shown below.", { exact: false }).waitFor(); });

await browser.close();
if (browserErrors.length) throw new Error(browserErrors.join("\n"));
console.log(`visual acceptance passed; screenshots: ${output}`);
