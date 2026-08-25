import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const baseUrl = process.env.QAMC_VISUAL_URL || "http://127.0.0.1:5173/cockpit/";
const output = resolve("../docs/verification/dashboard-finish-line");
await mkdir(output, { recursive: true });

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
  await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true });
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

await browser.close();
if (browserErrors.length) throw new Error(browserErrors.join("\n"));
console.log(`visual acceptance passed; screenshots: ${output}`);
