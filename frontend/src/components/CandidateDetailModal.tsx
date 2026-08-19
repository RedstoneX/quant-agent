import { useEffect, useState } from "react";
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
  TradeItem,
  TradeDecision,
} from "../api/client";
import { fmtMoney, fmtNum } from "../lib/format";
import { Modal, CrumbLink } from "./ui/Modal";
import { Pill } from "./ui/Pill";
import { Card, KV, CardText, EvidenceSection } from "./ui/Evidence";
import { StateMessage } from "./ui/Panel";
import { useModalActions } from "../context/ModalContext";
import { SpecialistCards } from "./SpecialistCards";
import { DecisionFlowDiagram, FlowStage, FlowStatus } from "./DecisionFlowDiagram";

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
        <table className="mt-2">
          <thead>
            <tr>
              <th>Sector</th>
              <th>Stance</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {macro.sector_guidance.map((g, i) => (
              <tr key={i}>
                <td>{g.sector}</td>
                <td>
                  <Pill text={g.stance} />
                </td>
                <td className="whitespace-normal">{g.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
    </Card>
  );
}

function numsDiffer(a: number | null | undefined, b: number | null | undefined): boolean {
  if (a === null || a === undefined || b === null || b === undefined) return false;
  return Math.abs(a - b) > 0.001;
}

function DeltaCell({ value, changed, fmt }: { value: number | null | undefined; changed: boolean; fmt: (v: number) => string }) {
  return (
    <td className={changed ? "text-warn font-bold" : ""}>
      {value === null || value === undefined ? "—" : fmt(value)}
    </td>
  );
}

function ProposedVsExecuted({ proposed, trade }: { proposed: TradeDecision | null; trade: TradeItem | null }) {
  if (!proposed && !trade) return null;
  const entryChanged = numsDiffer(proposed?.entry_price, trade?.price);
  const stopChanged = numsDiffer(proposed?.stop_loss, trade?.stop_loss);
  return (
    <table className="mt-2">
      <thead>
        <tr>
          <th></th>
          <th>Proposed (PM)</th>
          <th>Executed (trade)</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Action</td>
          <td>{proposed?.action || "—"}</td>
          <td>{trade?.action || "—"}</td>
        </tr>
        <tr>
          <td>Size</td>
          <td>{proposed ? `${fmtNum(proposed.allocation_pct)}% alloc` : "—"}</td>
          <td>{trade?.qty !== null && trade?.qty !== undefined ? `${fmtNum(trade.qty)} sh` : "—"}</td>
        </tr>
        <tr>
          <td>Entry / Fill price</td>
          <DeltaCell value={proposed?.entry_price} changed={entryChanged} fmt={fmtMoney} />
          <DeltaCell value={trade?.price} changed={entryChanged} fmt={fmtMoney} />
        </tr>
        <tr>
          <td>Stop loss</td>
          <DeltaCell value={proposed?.stop_loss} changed={stopChanged} fmt={fmtMoney} />
          <DeltaCell value={trade?.stop_loss} changed={stopChanged} fmt={fmtMoney} />
        </tr>
      </tbody>
    </table>
  );
}

/* ------------------------------------------------------------------ *
 * Decision flow: Specialists -> Portfolio Manager -> AI Risk Manager ->
 * Deterministic Gate -> Execution. buildCandidateStages derives every
 * stage's reached/outcome status purely from fields CandidateDetailResponse
 * already carries (cross-checked against the run funnel's per-candidate
 * `executed`/`hard_risk_block` when that supplementary fetch succeeds) —
 * never a fabricated guess about a stage Mission Control has no evidence
 * for. See src/api/db_reads.py::is_executed_trade for the exact predicate
 * `isExecutedTrade` below mirrors.
 * ------------------------------------------------------------------ */

// TradeItem (the Mission Control API's trade shape) doesn't expose the raw
// `fill_qty` column src/api/db_reads.py::is_executed_trade also checks —
// only fill_status/action are visible here, so this fallback uses those two
// (still exact for the common no-fill-tracking and explicit-fill cases; the
// funnel's own per-candidate `executed` boolean is preferred over this
// fallback whenever the supplementary funnel fetch succeeds).
function isExecutedTrade(trade: TradeItem | null): boolean {
  if (!trade) return false;
  return (trade.fill_status === null && trade.action !== "HOLD") || trade.fill_status === "filled";
}

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

function buildCandidateStages(detail: CandidateDetailResponse, funnel: RunFunnelResponse | null): FlowStage[] {
  const specialistCount = (detail.tech ? 1 : 0) + (detail.earnings ? 1 : 0) + detail.news_symbol.length;
  const specialistsReached = specialistCount > 0;

  const pmReached = !!(detail.pm_target || detail.pm_proposed_order || detail.pm_reasoning?.portfolio_view);
  const pmCaption = detail.pm_target
    ? `Target ${fmtNum(detail.pm_target.target_weight_pct)}%`
    : detail.pm_proposed_order
    ? detail.pm_proposed_order.action
    : undefined;

  const verdict = detail.risk_verdict?.verdict ?? null;
  let riskStatus: FlowStatus = "not_reached";
  if (verdict) {
    const hasMods = !!detail.risk_modification || verdict.modifications.length > 0;
    riskStatus = verdict.approved === false ? "rejected" : hasMods ? "modified" : "approved";
  }
  const riskCaption = verdict?.reason_category ? verdict.reason_category.replace(/_/g, " ") : undefined;

  const hardBlocked = funnel?.hard_risk_block === true;
  const funnelCandidate = funnel?.candidates.find((c) => c.symbol === detail.symbol) ?? null;
  const executed = funnelCandidate ? funnelCandidate.executed : isExecutedTrade(detail.trade);

  let gateStatus: FlowStatus = "not_reached";
  let gateCaption: string | undefined;
  if (hardBlocked) {
    gateStatus = "blocked";
    gateCaption = "Hard-risk gate blocked every candidate this run before the AI Risk Manager was called.";
  } else if (detail.trade) {
    gateStatus = "reached";
    gateCaption = "Cleared for execution.";
  } else if (verdict?.approved === true) {
    gateStatus = "pending";
    gateCaption = "Approved upstream; no trade recorded — gate outcome not separately exposed to Mission Control.";
  } else if (verdict?.approved === false) {
    gateCaption = "Rejected upstream by the AI Risk Manager — never reached the deterministic gate.";
  } else {
    gateCaption = "Not reached — no usable AI Risk Manager verdict recorded for this run.";
  }

  let execStatus: FlowStatus = "not_reached";
  let execCaption: string | undefined;
  if (executed) {
    execStatus = "executed";
    execCaption = detail.trade
      ? `${detail.trade.action}${detail.trade.qty !== null && detail.trade.qty !== undefined ? ` ${fmtNum(detail.trade.qty)}sh` : ""}`
      : undefined;
  } else if (verdict?.approved === false) {
    execCaption = "No trade — rejected by the AI Risk Manager before execution.";
  } else if (detail.pm_proposed_order) {
    execCaption = "Proposed but not executed this run (or a HOLD).";
  } else {
    execCaption = "No proposal reached execution.";
  }

  return [
    {
      key: "specialists",
      label: "Specialists",
      status: specialistsReached ? "reached" : "not_reached",
      caption: specialistsReached ? `${specialistCount} signal${specialistCount === 1 ? "" : "s"}` : "No evidence recorded",
    },
    { key: "pm", label: "Portfolio Manager", status: pmReached ? "reached" : "not_reached", caption: pmCaption },
    { key: "risk", label: "AI Risk Manager", status: riskStatus, caption: riskCaption },
    { key: "gate", label: "Deterministic Gate", status: gateStatus, caption: gateCaption },
    { key: "exec", label: "Execution", status: execStatus, caption: execCaption },
  ];
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
}: {
  detail: CandidateDetailResponse;
  funnel: RunFunnelResponse | null;
  riskLog: AgentLogItem | null;
}) {
  const stages = buildCandidateStages(detail, funnel);
  const gate = stages.find((s) => s.key === "gate");
  const exec = stages.find((s) => s.key === "exec");
  const pmReached = stages.find((s) => s.key === "pm")?.status !== "not_reached";

  const verdict = detail.risk_verdict?.verdict ?? null;
  const outcome = riskOutcome(detail);
  const pmChain = detail.pm_reasoning?.reasoning_chain ?? null;

  return (
    <div>
      <DecisionFlowDiagram stages={stages} />

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
          {detail.risk_verdict ? (
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
              <KV label="Price" value={fmtMoney(detail.trade.price)} />
              <div className="kv-row">
                <span className="text-dim">Fill status</span>
                <Pill text={detail.trade.fill_status || "unfilled"} />
              </div>
              {detail.trade.reasoning && <CardText text={detail.trade.reasoning} />}
              <ProposedVsExecuted proposed={detail.pm_proposed_order} trade={detail.trade} />
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
      {detail && (
        <div>
          <EvidenceSection title="Consensus">{[<SpecialistCards key="specialists" detail={detail} />]}</EvidenceSection>
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
            {[<DecisionDetail key="chain" detail={detail} funnel={funnel} riskLog={riskLog} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
