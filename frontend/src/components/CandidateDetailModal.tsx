import { useEffect, useState } from "react";
import { Card as TremorCard, Grid, Metric, Text, Badge } from "@tremor/react";
import {
  api,
  AgentLogItem,
  CandidateDetailResponse,
  EarningsAnalysis,
  MacroBroaderContext,
  NewsBroaderContext,
  NewsSymbolItem,
  ReasoningChainLike,
  RunDetailResponse,
  RunFunnelResponse,
  TechAnalysisResult,
} from "../api/client";
import { fmtMoney, fmtNum } from "../lib/format";
import { Modal, CrumbLink } from "./ui/Modal";
import { Pill } from "./ui/Pill";
import { Card, KV, CardText, EvidenceSection } from "./ui/Evidence";
import { StateMessage } from "./ui/Panel";
import { useModalActions } from "../context/ModalContext";
import type { FlowStage } from "./agentflow/types";
import { AgentFlowGraph } from "./agentflow/AgentFlowGraph";
import { buildCandidateGraph } from "./agentflow/buildGraph";
import { buildCandidateStages, furthestReachedStage } from "./funnelShared";
import { LifecycleTimeline } from "./LifecycleTimeline";
import { TradeTable } from "./TradesPanel";
export { buildCandidateStages, furthestReachedStage, skipText } from "./funnelShared";

function TechCard({ tech }: { tech: TechAnalysisResult | null }) {
  if (!tech) return null;
  return (
    <Card title="Technical analysis">
      <div className="kv-row">
        <span className="text-dim">Rating</span>
        <Pill text={tech.rating} />
      </div>
      <KV label="Conviction" value={tech.conviction} />
      <KV label="Entry" value={tech.entry_price !== null ? fmtMoney(tech.entry_price) : null} />
      <KV label="Reference target" value={tech.reference_target !== null ? fmtMoney(tech.reference_target) : null} />
      <KV label="Stop loss" value={tech.stop_loss !== null ? fmtMoney(tech.stop_loss) : null} />
      <KV label="Signal age (days)" value={tech.signal_age_days} />
      <CardText text={tech.reasoning} />
      {tech.thesis_invalid_if && <CardText text={`Invalid if: ${tech.thesis_invalid_if}`} dim />}
    </Card>
  );
}

function earningsFlags(earnings: EarningsAnalysis): string[] {
  const f = earnings.risk_flags;
  if (Array.isArray(f)) return f;
  return [...(f.strategic_risks || []), ...(f.operational_risks || [])];
}

function EarningsCard({ earnings }: { earnings: EarningsAnalysis | null }) {
  if (!earnings) return null;
  const impl = earnings.investment_implications;
  const flags = earningsFlags(earnings);
  return (
    <Card title="Earnings / filing analysis">
      <KV label="Form" value={earnings.form_type} />
      <KV label="Filing date" value={earnings.filing_date} />
      <div className="kv-row">
        <span className="text-dim">Sentiment</span>
        <Pill text={impl.sentiment} />
      </div>
      <KV label="Conviction" value={impl.conviction} />
      <CardText text={impl.key_thesis} />
      {impl.bull_case && impl.bull_case !== "not disclosed" && <CardText text={`Bull case: ${impl.bull_case}`} />}
      {impl.bear_case && impl.bear_case !== "not disclosed" && <CardText text={`Bear case: ${impl.bear_case}`} />}
      {flags.length > 0 && (
        <ul className="mt-1.5 pl-4 text-[0.79rem] list-disc">
          {flags.map((f, i) => (
            <li key={i}>{f}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function NewsSymbolCards({ items }: { items: NewsSymbolItem[] }) {
  if (!items.length) return null;
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
      {items.map((n, i) => (
        <Card key={i} title={n.headline}>
          <div className="kv-row">
            <span className="text-dim">Sentiment</span>
            <Pill text={n.sentiment} />
          </div>
          <KV label="Conviction" value={n.conviction} />
          <CardText text={n.impact_summary} />
        </Card>
      ))}
    </div>
  );
}

function MacroCard({ macro }: { macro: MacroBroaderContext | null }) {
  if (!macro) return null;
  return (
    <Card title="Macro regime context" broader>
      <KV label="Regime" value={macro.regime} />
      <KV label="Equity outlook" value={macro.equity_outlook} />
      <KV label="Confidence" value={macro.confidence} />
      {macro.summary && <CardText text={macro.summary} />}
      {macro.sector_guidance.length > 0 && (
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {macro.sector_guidance.map((guidance, index) => (
            <TremorCard key={`${guidance.sector}:${index}`} className="!bg-panel-alt !p-3 !ring-border">
              <div className="flex items-center justify-between gap-2"><span className="font-semibold">{guidance.sector}</span><Pill text={guidance.stance} /></div>
              <Text className="mt-1 text-sm text-ink">{guidance.reason}</Text>
            </TremorCard>
          ))}
        </div>
      )}
    </Card>
  );
}

function NewsContextCard({ news }: { news: NewsBroaderContext | null }) {
  if (!news) return null;
  return (
    <Card title="News / market narrative context" broader>
      <KV label="Market sentiment" value={news.market_sentiment} />
      <KV label="Confidence" value={news.confidence} />
      <KV label="Current regime" value={news.current_regime} />
      {news.pm_briefing && <CardText text={news.pm_briefing} />}
      {news.era_themes.length > 0 && (
        <ul className="mt-1.5 pl-4 text-[0.79rem] list-disc">
          {news.era_themes.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      )}
      {news.relevant_state_changes.length > 0 && (
        <div className="mt-2 space-y-2">
          {news.relevant_state_changes.map((change, index) => (
            <TremorCard key={`${change.event}:${index}`} className="!bg-panel-alt !p-3 !ring-border">
              <div className="font-semibold">{change.event}</div>
              <Text className="mt-1">{change.previous_state} &rarr; {change.new_state}</Text>
              <Text className="mt-1 text-sm text-ink">{change.market_impact}</Text>
            </TremorCard>
          ))}
        </div>
      )}
    </Card>
  );
}

function StopAndExecutionTruth({ detail, funnel }: { detail: CandidateDetailResponse; funnel: RunFunnelResponse | null }) {
  const proposed = detail.pm_proposed_order;
  const trade = detail.trades?.find((item) => item.action === proposed?.action) ?? detail.trade;
  const candidate = funnel?.candidates.find((item) => item.symbol === detail.symbol);
  const protection = [...(detail.pipeline_events ?? [])].reverse().find((event) => event.stage === "protection");
  if (!proposed && !trade && !protection) return null;
  return (
    <div className="mt-3">
      <Text className="mb-2 uppercase tracking-wide">Execution and protection truth</Text>
      <Grid numItems={1} numItemsSm={3} className="gap-2">
        <TremorCard className="!bg-panel-alt !p-3 !ring-border">
          <Text>PM proposed stop</Text>
          <Metric className="font-mono text-lg text-ink">{fmtMoney(proposed?.stop_loss)}</Metric>
          <Text className="mt-1 text-xs">Model proposal only</Text>
        </TremorCard>
        <TremorCard className="!bg-panel-alt !p-3 !ring-border">
          <Text>Execution-recorded stop</Text>
          <Metric className="font-mono text-lg text-ink">{fmtMoney(trade?.stop_loss)}</Metric>
          <Text className="mt-1 text-xs">Persisted trade record; not broker proof</Text>
        </TremorCard>
        <TremorCard className="!bg-panel-alt !p-3 !ring-border">
          <Text>Protection outcome</Text>
          <div className="mt-1"><Badge color={protection?.outcome === "placed" || candidate?.protection_outcome === "placed" ? "emerald" : "slate"}>{protection?.outcome || candidate?.protection_outcome || "not recorded"}</Badge></div>
          <Text className="mt-1 text-xs">Canonical protection event; no unproved live-stop claim</Text>
        </TremorCard>
      </Grid>
    </div>
  );
}

/* Decision flow: Specialists -> Portfolio Manager -> AI Risk Manager ->
 * Deterministic Gate -> Execution. funnelShared.buildCandidateStages
 * derives every stage's reached/outcome status purely from fields
 * CandidateDetailResponse already carries (cross-checked against the run
 * funnel's per-candidate `executed`/`hard_risk_block` when that
 * supplementary fetch succeeds) — never a fabricated guess about a stage
 * Mission Control has no evidence for. Also feeds DecisionSummaryLine.tsx's
 * one-line cockpit summary (via furthestReachedStage), which is all that
 * remains of this chain outside this modal now that the Decision Room
 * panel has been removed from the cockpit entirely. */

// "clean" = approved untouched, "modified" = approved with a modification,
// "rejected" = not approved. Exactly the derivation the product brief
// specifies from risk_verdict.verdict.approved + modifications/risk_modification.
function riskOutcome(detail: CandidateDetailResponse): "clean" | "modified" | "rejected" | null {
  const v = detail.risk_verdict?.verdict;
  if (!v) return null;
  if (v.approved === false) return "rejected";
  const hasMods = !!detail.risk_modification || v.modifications.length > 0;
  return hasMods ? "modified" : "clean";
}

function OutcomeBanner({ detail, stages, executed }: { detail: CandidateDetailResponse; stages: FlowStage[]; executed: boolean }) {
  const actionWord = detail.trade?.action || detail.pm_proposed_order?.action;
  if (executed) {
    const trade = detail.trade;
    return (
      <div className="rounded-xl border-2 border-pos/50 bg-pos/8 px-4 py-3 mb-3.5">
        <div className="text-[1.05rem] font-extrabold text-pos tracking-tight">
          FINAL OUTCOME · EXECUTED{actionWord ? ` — ${actionWord}` : ""}
        </div>
        {trade && (
          <p className="text-[0.85rem] mt-1">
            {trade.qty !== null && trade.qty !== undefined ? `${fmtNum(trade.qty)} sh` : ""} @ {fmtMoney(trade.price)}
            {trade.fill_status ? ` (${trade.fill_status})` : ""}
          </p>
        )}
      </div>
    );
  }

  const furthest = furthestReachedStage(stages);
  const reason = furthest?.caption || "Candidate-specific reason was not recorded.";
  return (
    <div className="rounded-xl border-2 border-warn/50 bg-warn/8 px-4 py-3 mb-3.5">
      <div className="text-[1.05rem] font-extrabold text-warn tracking-tight">
        FINAL OUTCOME · NOT EXECUTED{actionWord ? ` (proposed ${actionWord})` : ""}
      </div>
      <p className="text-[0.82rem] mt-1">
        <span className="text-dim">Stopped at: </span>
        <span className="font-semibold">{furthest ? furthest.label : "Specialists"}</span>
      </p>
      <p className="text-[0.85rem] mt-0.5">
        <span className="text-dim">Recorded reason: </span>
        {reason}
      </p>
    </div>
  );
}


// PM's 7(+2)-step CoT (src/models.py::ReasoningChain) and the Risk
// Manager's 6-step CoT (src/models.py::RiskReasoningChain) — human labels
// for whatever keys are actually present. Unknown/extra keys still render
// (fallback to the raw key), so this never hides evidence the backend sends.
const PM_CHAIN_LABELS: Record<string, string> = {
  macro_filter: "Macro filter",
  news_check: "News check",
  earnings_check: "Earnings check",
  signal_conflicts: "Signal conflicts",
  sizing_logic: "Sizing logic",
  portfolio_balance: "Portfolio balance",
  cash_target: "Cash target",
  continuity_check: "Continuity check (7-day arc)",
  premortem_check: "Pre-mortem (disconfirming case)",
};

const RISK_CHAIN_LABELS: Record<string, string> = {
  rr_audit: "R/R audit",
  signal_fidelity: "Signal fidelity",
  correlation_check: "Correlation check",
  event_risk: "Event risk",
  sizing_sanity: "Sizing sanity",
  overall: "Overall synthesis",
};

function ChainList({ chain, labels }: { chain: ReasoningChainLike | null | undefined; labels: Record<string, string> }) {
  const entries = chain
    ? Object.entries(chain).filter((e): e is [string, string] => typeof e[1] === "string" && e[1].trim() !== "")
    : [];
  if (!entries.length) return <StateMessage text="No reasoning chain recorded." />;
  return (
    <div className="flex flex-col gap-1.5 mt-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="text-[0.78rem]">
          <span className="font-semibold text-dim">{labels[k] || k}: </span>
          <span>{v}</span>
        </div>
      ))}
    </div>
  );
}

function AuditFlag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`text-[0.68rem] font-semibold ${ok ? "text-pos" : "text-dim"}`}>
      {ok ? "✓" : "—"} {label}
    </span>
  );
}

// Not a structured API field — the AI Risk Manager's runtime position-age/
// drawdown audit context lives only in the risk_manager agent_logs row's
// input_summary text (RunDetailResponse.agent_logs, fetched separately from
// CandidateDetailResponse). Clearly labeled as raw log text, not a
// candidate-specific structured field, and silently absent when unavailable.
function RiskAuditContextCard({ log }: { log: AgentLogItem | null }) {
  if (!log || !log.input_summary) return null;
  return (
    <details className="card mt-2">
      <summary className="font-bold text-[0.85rem] cursor-pointer select-none">
        Audit context the Risk Manager reviewed
      </summary>
      <p className="text-[0.7rem] text-dim mt-1.5">
        From this run&rsquo;s risk_manager model-call log (input_summary) — not a separate structured field, and
        applies to the whole run, not only this symbol.
      </p>
      <div className="text-[0.78rem] mt-1.5 whitespace-pre-wrap leading-snug">{log.input_summary}</div>
    </details>
  );
}

function DecisionDetail({
  detail,
  funnel,
  riskLog,
  stages,
}: {
  detail: CandidateDetailResponse;
  funnel: RunFunnelResponse | null;
  riskLog: AgentLogItem | null;
  stages: FlowStage[];
}) {
  const gate = stages.find((s) => s.key === "gate");
  const exec = stages.find((s) => s.key === "exec");
  const pmReached = stages.find((s) => s.key === "pm")?.status !== "not_reached";
  const reachedProposedOrder = !!detail.pm_proposed_order;

  const verdict = detail.risk_verdict?.verdict ?? null;
  const outcome = riskOutcome(detail);
  const pmChain = detail.pm_reasoning?.reasoning_chain ?? null;

  // Specialist nodes stay short (identity + threshold-colored confidence +
  // a reasoning excerpt) — clicking one scrolls to the full structured
  // evidence card below (TechCard/EarningsCard/NewsSymbolCards) rather than
  // cramming full CoT text into a graph node.
  const scrollToEvidence = () =>
    document.getElementById("symbol-specific-evidence")?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div>
      <AgentFlowGraph {...buildCandidateGraph(detail, stages, () => scrollToEvidence())} height={300} />

      <div className="flex flex-col gap-3 mt-3.5">
        <div>
          <div className="font-bold text-[0.85rem] mb-1">Portfolio Manager</div>
          {pmReached ? (
            <div className="card">
              {detail.pm_reasoning?.portfolio_view && <CardText text={detail.pm_reasoning.portfolio_view} />}
              {detail.pm_target && (
                <div className="mt-2">
                  <KV label="Target weight" value={`${fmtNum(detail.pm_target.target_weight_pct)}%`} />
                  <KV label="Conviction" value={detail.pm_target.conviction} />
                  <CardText text={detail.pm_target.thesis} />
                </div>
              )}
              {detail.pm_proposed_order && (
                <div className="mt-2">
                  <div className="kv-row">
                    <span className="text-dim">Constructed order</span>
                    <Pill text={detail.pm_proposed_order.action} />
                  </div>
                  <KV label="Allocation" value={`${fmtNum(detail.pm_proposed_order.allocation_pct)}%`} />
                  <KV label="Entry" value={fmtMoney(detail.pm_proposed_order.entry_price)} />
                  <CardText text={detail.pm_proposed_order.reasoning} />
                </div>
              )}
              {pmChain && (
                <div className="flex gap-3 flex-wrap mt-2">
                  <AuditFlag ok={!!pmChain.continuity_check?.trim()} label="Continuity check considered" />
                  <AuditFlag ok={!!pmChain.premortem_check?.trim()} label="Pre-mortem considered" />
                </div>
              )}
              <details className="mt-2">
                <summary className="text-[0.75rem] font-semibold cursor-pointer select-none text-dim">
                  Portfolio Manager reasoning chain
                </summary>
                <ChainList chain={pmChain} labels={PM_CHAIN_LABELS} />
              </details>
            </div>
          ) : (
            <StateMessage text="Portfolio Manager did not reach a target or order for this candidate this run." />
          )}
        </div>

        <div>
          <div className="font-bold text-[0.85rem] mb-1">AI Risk Manager</div>
          {!reachedProposedOrder ? (
            <StateMessage text="This candidate never reached a Portfolio Manager proposed order — the AI Risk Manager evaluates proposed orders, so it did not evaluate this candidate. Any Risk verdict recorded for this run belongs to a different, proposed candidate." />
          ) : detail.risk_verdict ? (
            verdict ? (
              <div className="card">
                <div className="kv-row">
                  <span className="text-dim">Verdict</span>
                  <div className="flex gap-1.5 flex-wrap justify-end">
                    <Pill text={verdict.approved ? "approved" : "rejected"} />
                    {outcome && outcome !== "rejected" && outcome !== verdict.reason_category && <Pill text={outcome} />}
                    <Pill text={verdict.reason_category} />
                  </div>
                </div>
                <CardText text={verdict.reasoning} />
                {verdict.modifications.length > 0 && (
                  <ul className="mt-1.5 pl-4 text-[0.79rem] list-disc">
                    {verdict.modifications.map((m, i) => (
                      <li key={i}>
                        {m.symbol}: {m.field} {m.original_value} &rarr; {m.new_value} ({m.reason})
                      </li>
                    ))}
                  </ul>
                )}
                {detail.risk_modification && (
                  <div className="mt-2">
                    <div className="text-[0.7rem] text-dim uppercase tracking-wide mb-1">Modification — this symbol</div>
                    <KV label="Field" value={detail.risk_modification.field} />
                    <KV label="Original" value={fmtNum(detail.risk_modification.original_value)} />
                    <KV label="Modified to" value={fmtNum(detail.risk_modification.new_value)} />
                    <CardText text={detail.risk_modification.reason} />
                  </div>
                )}
                <details className="mt-2">
                  <summary className="text-[0.75rem] font-semibold cursor-pointer select-none text-dim">
                    Risk Manager reasoning chain
                  </summary>
                  <ChainList chain={verdict.reasoning_chain} labels={RISK_CHAIN_LABELS} />
                </details>
                <RiskAuditContextCard log={riskLog} />
              </div>
            ) : (
              <StateMessage text="Verdict recorded but could not be read back." />
            )
          ) : (
            <StateMessage text="AI Risk Manager was not reached for this candidate this run." />
          )}
        </div>

        <div>
          <div className="font-bold text-[0.85rem] mb-1">Deterministic gate</div>
          <StateMessage text={gate?.caption || "No further detail recorded."} />
        </div>

        <div>
          <div className="font-bold text-[0.85rem] mb-1">Execution</div>
          {detail.trade ? (
            <div className="card">
              <div className="kv-row">
                <span className="text-dim">Action</span>
                <Pill text={detail.trade.action} />
              </div>
              <KV label="Qty" value={detail.trade.qty !== null && detail.trade.qty !== undefined ? fmtNum(detail.trade.qty) : null} />
              <KV label="Requested price" value={fmtMoney(detail.trade.price)} />
              <KV label="Filled quantity" value={fmtNum(detail.trade.fill_qty)} />
              <KV label="Fill price" value={fmtMoney(detail.trade.fill_price)} />
              <KV label="Realized P&L" value={detail.trade.realized_pnl === null || detail.trade.realized_pnl === undefined ? null : fmtMoney(detail.trade.realized_pnl)} />
              <div className="kv-row">
                <span className="text-dim">Fill status</span>
                <Pill text={detail.trade.fill_status || "unfilled"} />
              </div>
              {detail.trade.reasoning && <CardText text={detail.trade.reasoning} />}
              <StopAndExecutionTruth detail={detail} funnel={funnel} />
              {(detail.trades?.length ?? 0) > 1 && (
                <div className="mt-3">
                  <Text className="mb-2 uppercase tracking-wide">All linked trade records</Text>
                  <TradeTable trades={detail.trades ?? []} />
                </div>
              )}
            </div>
          ) : (
            <StateMessage text={exec?.caption || "No trade recorded for this candidate this run."} />
          )}
        </div>
      </div>
    </div>
  );
}

export function CandidateDetailModal({
  runId,
  symbol,
  onClose,
}: {
  runId: string;
  symbol: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<CandidateDetailResponse | null>(null);
  const [funnel, setFunnel] = useState<RunFunnelResponse | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { openRunDetail } = useModalActions();

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setFunnel(null);
    setRunDetail(null);
    setError(null);

    api
      .candidateDetail(runId, symbol)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });

    // Supplementary, non-fatal fetches: cross-check the deterministic-gate
    // outcome (hard_risk_block, per-candidate `executed`) and surface the
    // AI Risk Manager's raw audit-context log. Neither blocks or replaces
    // the primary candidateDetail fetch above if it fails.
    api
      .runFunnel(runId)
      .then((f) => {
        if (!cancelled) setFunnel(f);
      })
      .catch(() => undefined);
    api
      .runDetail(runId)
      .then((d) => {
        if (!cancelled) setRunDetail(d);
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, [runId, symbol]);

  const riskLog = runDetail?.agent_logs.find((a) => a.agent_name === "risk_manager") ?? null;
  const stages = detail ? buildCandidateStages(detail, funnel) : null;
  const executed = stages?.find((stage) => stage.key === "exec")?.status === "executed";

  return (
    <Modal
      breadcrumb={
        <>
          <CrumbLink text={`Run ${runId}`} onClick={() => openRunDetail(runId)} />
          <span className="text-dim">/</span>
          <span className="font-bold">{symbol}</span>
        </>
      }
      onClose={onClose}
    >
      {error && <StateMessage text={`Could not load ${symbol}: ${error}`} error />}
      {!error && !detail && <StateMessage text={`Loading ${symbol}…`} />}
      {detail && stages && (
        <div>
          <OutcomeBanner detail={detail} stages={stages} executed={executed} />

          {/* Specialist identity/direction/confidence now lives in the
              agent-topology graph inside "Decision flow" below (real
              fan-in, not a separate near-duplicate card grid) — clicking a
              specialist node there scrolls to this full structured-evidence
              section for the complete reasoning text. */}
          <div id="symbol-specific-evidence" />
          <EvidenceSection title="Symbol-specific evidence">
            {[
              <TechCard key="tech" tech={detail.tech} />,
              <EarningsCard key="earn" earnings={detail.earnings} />,
              <NewsSymbolCards key="news" items={detail.news_symbol} />,
            ]}
          </EvidenceSection>
          <EvidenceSection title="Broader context (not symbol-specific)">
            {[<MacroCard key="macro" macro={detail.macro_context} />, <NewsContextCard key="newsctx" news={detail.news_context} />]}
          </EvidenceSection>
          <EvidenceSection title="Decision flow: Specialists &rarr; PM &rarr; AI Risk &rarr; gate &rarr; execution">
            {[<DecisionDetail key="chain" detail={detail} funnel={funnel} riskLog={riskLog} stages={stages} />]}
          </EvidenceSection>
          <EvidenceSection title="Persisted lifecycle: opportunity &rarr; result">
            {[<LifecycleTimeline key="lifecycle" events={detail.pipeline_events ?? []} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
