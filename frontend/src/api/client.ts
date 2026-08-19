// Typed fetch wrappers mirroring src/api/schemas.py field-for-field.
// Keep the two in sync by hand — this is the one place a backend
// contract change needs a matching frontend edit.

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------
// /health
// ---------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  db_reachable: boolean;
  broker_reachable: boolean | null;
  paper: boolean | null;
  sessions_logged_today: string[];
  last_run_files: Record<string, string | null>;
  session_lock_active: boolean | null;
  timestamp: string;
}

// ---------------------------------------------------------------------
// /account, /positions, /orders, /trades, /prices
// ---------------------------------------------------------------------

export interface DailyPnlPoint {
  date: string;
  total_value: number | null;
  daily_pnl: number | null;
  daily_return_pct: number | null;
  equity_close: number | null;
}

export interface LiquidityBreakdown {
  sweep_enabled: boolean;
  sweep_symbol: string | null;
  raw_cash: number | null;
  sweep_parked_value: number | null;
  reserve_usd: number | null;
  deployable_cash: number | null;
  total_liquidity: number | null;
}

export interface AccountResponse {
  cash: number | null;
  portfolio_value: number | null;
  last_equity: number | null;
  daily_pnl: number | null;
  daily_pnl_pct: number | null;
  paper: boolean | null;
  history: DailyPnlPoint[];
  liquidity: LiquidityBreakdown | null;
  error: string | null;
}

export type PositionDirection = "long" | "bearish_hedge" | "cash_equivalent";

export interface PositionItem {
  symbol: string;
  qty: number;
  avg_entry: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_intraday_pnl: number | null;
  sector: string | null;
  is_cash_equivalent: boolean;
  direction: PositionDirection;
}

export interface PositionsResponse {
  positions: PositionItem[];
  error: string | null;
}

export interface OrderItem {
  id: string;
  symbol: string;
  side: string | null;
  qty: number | null;
  order_type: string | null;
  status: string | null;
  limit_price: number | null;
  stop_price: number | null;
  filled_qty: number | null;
  filled_avg_price: number | null;
  submitted_at: string | null;
  filled_at: string | null;
}

export interface OrdersResponse {
  orders: OrderItem[];
  error: string | null;
}

export interface TradeItem {
  id: number;
  symbol: string;
  action: string;
  qty: number | null;
  price: number | null;
  reasoning: string | null;
  run_id: string | null;
  decision_id: string | null;
  fill_status: string | null;
  timestamp: string | null;
  stop_loss: number | null;
  take_profit: number | null;
}

export interface TradesResponse {
  trades: TradeItem[];
  count: number;
}

export interface PriceBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PriceBarsResponse {
  symbol: string;
  bars: PriceBar[];
  error: string | null;
}

// ---------------------------------------------------------------------
// /runs, /runs/{id}, /runs/{id}/candidates(/{symbol}), /runs/{id}/funnel
// ---------------------------------------------------------------------

export interface AgentLogItem {
  id: number;
  agent_name: string;
  decision_id: string | null;
  timestamp: string | null;
  input_summary: string | null;
  output_summary: string | null;
  requested_provider: string | null;
  requested_model: string | null;
  actual_provider: string | null;
  model: string | null;
  status: string | null;
  cost_usd: number | null;
  latency_s: number | null;
  tokens_used: number | null;
}

export interface RunSummary {
  run_id: string;
  session_prefix: string | null;
  first_timestamp: string | null;
  last_timestamp: string | null;
  agent_count: number;
  decision_id: string | null;
  total_cost_usd: number | null;
}

export interface RunsResponse {
  runs: RunSummary[];
}

export interface RunDetailResponse {
  run_id: string;
  agent_logs: AgentLogItem[];
  trades: TradeItem[];
  decision_id: string | null;
  total_cost_usd: number | null;
  hard_risk_block_recorded: boolean;
}

export interface RunCandidatesResponse {
  run_id: string;
  candidates: string[];
}

export interface ReasoningChainLike {
  [key: string]: string | undefined;
}

export interface TechAnalysisResult {
  symbol: string;
  rating: "strong_buy" | "buy" | "neutral" | "sell" | "strong_sell";
  conviction: "high" | "medium" | "low";
  entry_price: number | null;
  reference_target: number | null;
  stop_loss: number | null;
  reasoning_chain: ReasoningChainLike;
  reasoning: string;
  thesis_invalid_if: string;
  signal_age_days: number | null;
}

export interface EarningsAnalysis {
  form_type: string;
  filing_date: string;
  investment_implications: {
    sentiment: "bullish" | "bearish" | "neutral";
    conviction: "high" | "medium" | "low";
    key_thesis: string;
    bull_case: string;
    bear_case: string;
  };
  risk_flags: string[] | { strategic_risks: string[]; operational_risks: string[] };
}

export interface NewsSymbolItem {
  headline: string;
  sentiment: "bullish" | "bearish" | "neutral";
  conviction: "high" | "medium" | "low";
  impact_summary: string;
}

export interface MacroBroaderContext {
  regime: string | null;
  equity_outlook: string | null;
  confidence: string | null;
  summary: string | null;
  sector_guidance: { sector: string; stance: string; reason: string }[];
  timestamp: string | null;
}

export interface NewsStateChange {
  event: string;
  previous_state: string;
  new_state: string;
  market_impact: string;
  affected_symbols: string[];
  conviction: "high" | "medium" | "low";
}

export interface NewsBroaderContext {
  market_sentiment: string | null;
  confidence: string | null;
  pm_briefing: string | null;
  era_themes: string[];
  current_regime: string | null;
  relevant_state_changes: NewsStateChange[];
  timestamp: string | null;
}

export interface PmReasoning {
  portfolio_view: string | null;
  reasoning_chain: ReasoningChainLike | null;
  timestamp: string | null;
}

export interface TargetPosition {
  symbol: string;
  target_weight_pct: number;
  conviction: "high" | "medium" | "low";
  thesis: string;
  thesis_invalid_if: string;
}

export interface TradeDecision {
  action: "BUY" | "SELL" | "HOLD";
  symbol: string;
  allocation_pct: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  reasoning: string;
}

// "clean" = approved untouched; the others each name the specific reason
// the AI Risk Manager modified, scaled, or rejected — PM reads recent
// history of this field to self-calibrate. See src/models.py::RiskVerdict.
export type RiskReasonCategory =
  | "clean"
  | "oversized"
  | "rr_fail"
  | "concentration"
  | "correlation_risk"
  | "event_risk"
  | "macro_misalign"
  | "data_degraded"
  | "signal_fidelity"
  | "other";

export interface RiskVerdict {
  approved: boolean;
  reasoning: string;
  reasoning_chain: ReasoningChainLike;
  reason_category: RiskReasonCategory;
  modifications: { symbol: string; field: string; original_value: number; new_value: number; reason: string }[];
  scale_all_buys: number;
}

export interface RiskManagerVerdict {
  verdict: RiskVerdict | null;
  timestamp: string | null;
}

export interface RiskModification {
  symbol: string;
  field: string;
  original_value: number;
  new_value: number;
  reason: string;
}

export interface ConsensusSignal {
  source: string;
  direction: "bullish" | "bearish" | "neutral";
  detail: string;
}

export interface ConsensusSummary {
  signals: ConsensusSignal[];
  agreement: "aligned" | "mixed" | "no_directional_signal" | "insufficient_data";
}

export interface CandidateDetailResponse {
  run_id: string;
  symbol: string;
  decision_id: string | null;
  tech: TechAnalysisResult | null;
  earnings: EarningsAnalysis | null;
  news_symbol: NewsSymbolItem[];
  macro_context: MacroBroaderContext | null;
  news_context: NewsBroaderContext | null;
  pm_reasoning: PmReasoning | null;
  pm_target: TargetPosition | null;
  pm_proposed_order: TradeDecision | null;
  risk_verdict: RiskManagerVerdict | null;
  risk_modification: RiskModification | null;
  trade: TradeItem | null;
  consensus: ConsensusSummary;
}

export type DecisionState =
  | "executed"
  | "proposed_not_executed"
  | "hard_risk_block"
  | "no_proposal"
  | "no_candidates";

export interface CandidateFunnelItem {
  symbol: string;
  direction: "bullish" | "bearish" | "neutral" | "unknown";
  is_bearish_hedge: boolean;
  reached_pm_target: boolean;
  pm_target_weight_pct: number | null;
  reached_proposed_order: boolean;
  proposed_action: string | null;
  risk_modified: boolean;
  executed: boolean;
  trade_action: string | null;
}

export interface RunFunnelResponse {
  run_id: string;
  session_prefix: string | null;
  timestamp: string | null;
  candidates: CandidateFunnelItem[];
  candidates_considered: number;
  reached_pm_count: number;
  proposed_order_count: number;
  executed_count: number;
  bearish_hedge_considered: boolean;
  hard_risk_block: boolean;
  pm_reasoning: PmReasoning | null;
  risk_verdict: RiskManagerVerdict | null;
  macro_context: MacroBroaderContext | null;
  decision_state: DecisionState;
}

// ---------------------------------------------------------------------
// /journal, /search
// ---------------------------------------------------------------------

export interface ReflectionItem {
  date: string;
  tomorrow_outlook: string | null;
  lessons: string | null;
  suggested_actions: string | null;
  risk_rating: string | null;
  tomorrow_bias: string | null;
  tomorrow_conviction: string | null;
  tomorrow_key_risks: string | null;
  sell_decisions_assessment: string | null;
  sell_grades_json: string | null;
  buy_grades_json: string | null;
  missed_opportunities_json: string | null;
  timestamp: string | null;
}

export interface JournalDatesResponse {
  dates: string[];
}

export interface JournalDayResponse {
  date: string;
  has_data: boolean;
  daily_pnl: DailyPnlPoint | null;
  reflection: ReflectionItem | null;
  runs: RunSummary[];
  trades: TradeItem[];
  candidates: string[];
}

export interface SearchTradeHit {
  kind: "trade";
  id: number;
  symbol: string;
  action: string;
  run_id: string | null;
  timestamp: string | null;
  reasoning: string | null;
}

export interface SearchAgentLogHit {
  kind: "agent_log";
  id: number;
  agent_name: string;
  run_id: string | null;
  timestamp: string | null;
  model: string | null;
  output_summary: string | null;
}

export interface SearchResponse {
  query: string;
  trades: SearchTradeHit[];
  agent_logs: SearchAgentLogHit[];
}

// ---------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------

export const api = {
  health: () => getJSON<HealthResponse>("/health"),
  account: () => getJSON<AccountResponse>("/account"),
  positions: () => getJSON<PositionsResponse>("/positions"),
  orders: (status: "open" | "closed" | "all" = "open") =>
    getJSON<OrdersResponse>(`/orders?status=${status}`),
  trades: (limit = 30) => getJSON<TradesResponse>(`/trades?limit=${limit}`),
  prices: (symbol: string, lookbackDays = 120) =>
    getJSON<PriceBarsResponse>(`/prices/${encodeURIComponent(symbol)}?lookback_days=${lookbackDays}`),
  runs: (limit = 25) => getJSON<RunsResponse>(`/runs?limit=${limit}`),
  runDetail: (runId: string) => getJSON<RunDetailResponse>(`/runs/${encodeURIComponent(runId)}`),
  runCandidates: (runId: string) =>
    getJSON<RunCandidatesResponse>(`/runs/${encodeURIComponent(runId)}/candidates`),
  runFunnel: (runId: string) =>
    getJSON<RunFunnelResponse>(`/runs/${encodeURIComponent(runId)}/funnel`),
  candidateDetail: (runId: string, symbol: string) =>
    getJSON<CandidateDetailResponse>(
      `/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(symbol)}`
    ),
  journalDates: (limit = 60) => getJSON<JournalDatesResponse>(`/journal/dates?limit=${limit}`),
  journalDay: (date: string) => getJSON<JournalDayResponse>(`/journal/${encodeURIComponent(date)}`),
  search: (q: string, limit = 50) =>
    getJSON<SearchResponse>(`/search?q=${encodeURIComponent(q)}&limit=${limit}`),
};
