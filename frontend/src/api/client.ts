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
  decision_path_status: string;
  // Can the desk still reach the operator? Written by every session
  // (src/alert_watchdog.py). "unknown" means no check has ever been
  // recorded — deliberately not the same as healthy.
  alert_channel: {
    status: "ok" | "broken" | "stale" | "unknown";
    last_check_at: string | null;
    last_ok_at: string | null;
    last_stage: string | null;
    last_detail: string | null;
    consecutive_failures: number;
    age_hours: number | null;
    stale_after_hours: number;
    error: string | null;
  } | null;
  llm_circuit: {
    available: boolean;
    suspended: boolean | null;
    suspension_class: "hard" | "quota" | null;
    hold_scope: "global" | "day" | "mode_day" | "session" | null;
    requires_operator_reset: boolean;
    auto_rearm: boolean;
    trigger: string | null;
    trigger_code: string | null;
    suspended_at: string | null;
    daily_cost_usd: number | null;
    daily_limit_usd: number | null;
    active_quota_holds: Array<{
      scope: "day" | "mode_day" | "session";
      day: string;
      trigger_code: string;
      trigger_detail: string;
      run_id: string;
      mode: string;
      agent_name: string;
      session_cost_usd: number;
      daily_cost_usd: number;
      created_at: string;
    }>;
    recent_recovery: {
      scope: string;
      day: string;
      trigger_code: string;
      mode: string;
      released_at: string;
      release_reason: string;
    } | null;
  } | null;
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

export interface RiskLimits {
  max_position_pct: number | null;
  max_total_position_pct: number | null;
  max_daily_loss_pct: number | null;
  max_sector_pct: number | null;
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
  risk_limits: RiskLimits | null;
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
  broker_order_id?: string | null;
  fill_status: string | null;
  fill_qty?: number | null;
  fill_price?: number | null;
  realized_pnl?: number | null;
  fill_reconciled_at?: string | null;
  timestamp: string | null;
  stop_loss: number | null;
  take_profit: number | null;
  position_id?: string | null;
  exit_reason_category?: string | null;
  // Conviction ledger (spec §7.2, PR #159) — pinned at ENTRY only, so
  // interim/exit rows and any row written before PR #159 carry null here,
  // never a fabricated default.
  conviction?: string | null;
  requested_risk_pct?: number | null;
  allocated_risk_pct?: number | null;
  decision_model?: string | null;
}

export interface TradesResponse {
  trades: TradeItem[];
  count: number;
}

export interface PositionHistoryResponse {
  position_id: string;
  symbol: string;
  status: "open" | "closed";
  entry: TradeItem;
  interim: TradeItem[];
  exit: TradeItem | null;
  realized_pnl_total: number | null;
  realized_pnl_partial: boolean;
  hold_days: number | null;
  trade_count: number;
}

export interface PriceBar {
  date: string;
  timestamp?: string | null;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PriceBarsResponse {
  symbol: string;
  timeframe: ChartTimeframe;
  bars: PriceBar[];
  error: string | null;
}

export type ChartTimeframe = "5m" | "15m" | "1h" | "1d";

// ---------------------------------------------------------------------
// /quotes — current-session quote facts, distinct from PositionItem's
// broker-marked current_price (held positions only) and PriceBar's
// historical daily bars (up to one session behind during market hours).
// See docs/architecture/MISSION_CONTROL_API.md "Mission Control
// data-truth" tranche.
// ---------------------------------------------------------------------

export interface LiveQuote {
  symbol: string;
  last_price: number | null;
  prev_close: number | null;
  session_open: number | null;
  session_high: number | null;
  session_low: number | null;
}

export interface LiveQuotesResponse {
  quotes: LiveQuote[];
  as_of: string;
  source: string;
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
  /**
   * The COMPLETE prompt this agent received — every memory layer, verbatim.
   * Persisted since the field was added and served by /runs/{id} all along;
   * it was simply missing from this interface, so no view could render it.
   * For the Portfolio Manager this is the assembled briefing (the 7-evening
   * narrative, recurring missed themes, repeat loss patterns, recent RM
   * verdicts, its own last decisions, win-rate calibration) that the operator
   * could previously only infer from its source material.
   *
   * Large: production PM prompts run 13KB-190KB. Render it collapsed.
   */
  input_message: string | null;
  output_summary: string | null;
  /** The agent's complete raw response, same size caveat as input_message. */
  full_response: string | null;
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
  suggested_stop_price?: number | null;
  catalyst?: string;
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
  trades?: TradeItem[];
  pipeline_events?: PipelineEvent[];
  consensus: ConsensusSummary;
}

export interface PipelineEvent {
  stage: string;
  outcome: string;
  reason: string;
  timestamp: string | null;
  details: Record<string, unknown>;
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
  /** Spec §2.1 risk-based sizing. Null for legacy notional targets, and
   *  `pm_target_weight_pct` is null for risk-sized ones — show whichever
   *  the PM actually stated rather than converting between them. */
  pm_risk_allocation_pct: number | null;
  reached_proposed_order: boolean;
  proposed_action: string | null;
  risk_modified: boolean;
  executed: boolean;
  trade_action: string | null;
  order_status?: string | null;
  fill_qty?: number | null;
  fill_price?: number | null;
  realized_pnl?: number | null;
  protection_outcome?: string | null;
  pipeline_events?: PipelineEvent[];
  // Why the execution phase deterministically dropped this candidate's
  // approved order, when it did — quoted from the persisted `execution_skip`
  // evidence (e.g. "insufficient_cash", "stale_entry"). None when no skip
  // was recorded (src/api/schemas.py::CandidateFunnelItem — this was
  // already returned by the backend; the frontend contract just didn't
  // expose it, so the UI fell back to a generic "proposed but not
  // executed" even when a specific reason existed).
  execution_skip_reason: string | null;
  execution_skip_detail: string | null;
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
  pipeline_events?: PipelineEvent[];
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
// /research/daily/{date} — editorial read-side synthesis only
// ---------------------------------------------------------------------

export type ResearchDirection = "bullish" | "bearish" | "neutral" | "mixed" | "unknown";
export type ResearchItemStatus = "current" | "aging" | "stale" | "historical" | "partial" | "unavailable" | "error" | "quiet";

export interface ResearchEvidenceItem {
  label: string;
  value: string;
  source?: string | null;
  timestamp?: string | null;
}

export interface ResearchAgentBrief {
  seat: string;
  status: ResearchItemStatus;
  headline: string | null;
  read: string | null;
  direction: ResearchDirection;
  evidence: ResearchEvidenceItem[];
  changed: string | null;
  tension: string | null;
  why_now: string | null;
  market_context: ResearchMarketContext[];
  timestamp: string | null;
  error?: string | null;
}

export interface ResearchMarketContext {
  symbol: string;
  stop: number;
  entry: number;
  target: number;
}

export interface ResearchSignal {
  seat: string;
  direction: ResearchDirection;
  signal: string | null;
  relationship: "agrees" | "conflicts" | "independent" | "unknown";
  timestamp: string | null;
}

export interface ResearchDecisionStep {
  stage: "read" | "portfolio_manager" | "ai_risk" | "deterministic_gate" | "execution";
  status: string;
  summary: string | null;
  detail: string | null;
  timestamp: string | null;
}

export interface SmartMoneyFinding {
  id: string;
  symbol: string | null;
  stream: string;
  headline: string;
  summary: string;
  classification: "actionable" | "confirmatory" | "contradictory" | "historical" | "admission";
  freshness: "real_time" | "timely" | "delayed" | "stale" | "unknown";
  event_timestamp: string | null;
  knowable_timestamp: string | null;
  lag_days: number | null;
  materiality: string | null;
  source_name: string;
  source_url: string | null;
  source_detail: string | null;
  direction: ResearchDirection;
  observation_count: number;
  admitted_this_run: boolean;
  admission_detail: string | null;
}

export interface ResearchReviews {
  daily_result: string | null;
  position_reviewer: string | null;
  evening_review: string | null;
  meta_reflection: string | null;
  lesson_learned: string | null;
  suggested_actions: string[];
  tomorrow_watch: string[];
}

export interface ResearchDeskData {
  date: string;
  status: ResearchItemStatus;
  as_of: string | null;
  prior_as_of: string | null;
  thesis: string | null;
  what_changed: string[];
  tension: string | null;
  why_now: string | null;
  dry_annotation: string | null;
  agents: ResearchAgentBrief[];
  signal_stack: ResearchSignal[];
  decision_run_id: string | null;
  decision_chain: ResearchDecisionStep[];
  smart_money: SmartMoneyFinding[];
  reviews: ResearchReviews | null;
  errors: string[];
}

export interface ResearchFreshness {
  latest_recorded_at: string | null;
  age_minutes: number | null;
  label: "current" | "aging" | "stale" | "historical" | "unknown";
}

export interface ResearchAgentCall {
  id: number;
  agent_name: string;
  run_id: string | null;
  decision_id: string | null;
  timestamp: string | null;
  status: string | null;
  output_summary: string | null;
  requested_provider: string | null;
  requested_model: string | null;
  actual_provider: string | null;
  model: string | null;
  prompt_version: string | null;
  latency_s: number | null;
  cost_usd: number | null;
  structured_evidence_count: number;
}

export interface StoredResearchEvidence {
  id: number;
  run_id: string;
  decision_id: string | null;
  agent_name: string;
  kind: string;
  scope: string;
  symbol: string | null;
  timestamp: string | null;
  state: "valid" | "invalid";
  payload: Record<string, unknown> | unknown[] | null;
}

export interface ResearchDecisionDeltaRaw {
  run_id: string;
  decision_id: string | null;
  state: "executed" | "hard_risk_block" | "proposed_not_executed" | "no_proposal";
  proposed: StoredResearchEvidence[];
  risk_changes: StoredResearchEvidence[];
  deterministic_events: StoredResearchEvidence[];
  trades: TradeItem[];
}

export interface ResearchRun {
  summary: RunSummary;
  agent_calls: ResearchAgentCall[];
  evidence: StoredResearchEvidence[];
  decision_delta: ResearchDecisionDeltaRaw;
}

export interface ResearchDailyResponse {
  date: string;
  as_of: string | null;
  state: "complete" | "partial" | "empty" | "error";
  freshness: ResearchFreshness;
  read_error: string | null;
  missing_sources: string[];
  daily_pnl: DailyPnlPoint | null;
  reflection: ReflectionItem | null;
  runs: ResearchRun[];
}

// ---------------------------------------------------------------------
// /analysts/scorecard — the conviction ledger, per analyst (spec §9.5)
//
// `credit` / `r_multiple` / `cumulative` are in R: profit as a multiple of
// the risk the position was opened with. The panel never shows R; it
// multiplies by `risk_dollars_per_call` and shows dollars.
// ---------------------------------------------------------------------

export interface ScorecardPoint {
  resolved_at: string;
  cumulative: number;
  peak: number;
  /** peak - cumulative, never negative: how far below its own best. */
  below_best: number;
}

export interface ScorecardMonthPoint {
  month: string; // "YYYY-MM"
  credit: number;
  cumulative: number;
  resolved_calls: number;
  calls_right: number;
  hit_rate_pct: number | null;
}

export interface ScorecardConfidenceBreakdown {
  /** "high" / "medium" / "low", or whatever else the analyst declared. */
  conviction: string;
  resolved_calls: number;
  calls_right: number;
  hit_rate_pct: number | null;
  avg_win: number | null;
  /** Negative, not a magnitude. */
  avg_loss: number | null;
  cumulative_credit: number;
}

export interface AnalystScorecardItem {
  analyst: string;
  resolved_calls: number;
  calls_right: number;
  hit_rate_pct: number | null;
  avg_win: number | null;
  /** Negative, not a magnitude. */
  avg_loss: number | null;
  cumulative_credit: number;
  peak: number;
  below_best: number;
  below_best_since: string | null;
  calls_since_peak: number;
  cumulative: ScorecardPoint[];
  monthly: ScorecardMonthPoint[];
  /** The same calls split by the confidence the analyst declared on each,
   * high first. Only levels it actually used appear. This replaced the
   * conviction weight: credit is raw and unweighted, so whether confident
   * calls are worth more is shown rather than asserted. */
  by_confidence: ScorecardConfidenceBreakdown[];
}

export interface ScorecardIdeaAnalyst {
  analyst: string;
  side: "supported" | "opposed";
  stance: string;
  /** What the analyst declared. Reported, never applied — it scales nothing. */
  conviction: string;
  /** Raw signed score in R: positive when this analyst's side made money,
   * negative when it lost. Identical convention for a short and a long. */
  credit: number;
  nominated: boolean;
  reason: string;
}

export interface ScorecardIdea {
  symbol: string;
  /** "long" | "short". Descriptive only — nothing inverts on it. */
  direction: string;
  position_id: string | null;
  decision_id: string | null;
  resolved_at: string;
  r_multiple: number;
  supported: ScorecardIdeaAnalyst[];
  opposed: ScorecardIdeaAnalyst[];
}

export interface AnalystScorecardResponse {
  as_of: string;
  state: "populated" | "empty" | "error";
  read_error: string | null;
  risk_dollars_per_call: number;
  resolved_calls_total: number;
  months: string[];
  analysts: AnalystScorecardItem[];
  ideas: ScorecardIdea[];
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
  positionHistory: (positionId: string) =>
    getJSON<PositionHistoryResponse>(`/positions/${encodeURIComponent(positionId)}/history`),
  prices: (symbol: string, lookbackDays = 120, timeframe: ChartTimeframe = "1d") =>
    getJSON<PriceBarsResponse>(
      `/prices/${encodeURIComponent(symbol)}?lookback_days=${lookbackDays}&timeframe=${timeframe}`
    ),
  quotes: (symbols: string[]) =>
    getJSON<LiveQuotesResponse>(`/quotes?symbols=${encodeURIComponent(symbols.join(","))}`),
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
  researchDaily: (date: string) =>
    getJSON<ResearchDailyResponse>(`/research/daily/${encodeURIComponent(date)}`),
  analystScorecard: (ideaLimit = 25) =>
    getJSON<AnalystScorecardResponse>(`/analysts/scorecard?idea_limit=${ideaLimit}`),
  search: (q: string, limit = 50) =>
    getJSON<SearchResponse>(`/search?q=${encodeURIComponent(q)}&limit=${limit}`),
};
