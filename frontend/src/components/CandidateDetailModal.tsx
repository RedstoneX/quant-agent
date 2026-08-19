import { useEffect, useState } from "react";
import {
  api,
  CandidateDetailResponse,
  ConsensusSummary,
  EarningsAnalysis,
  MacroBroaderContext,
  NewsBroaderContext,
  NewsSymbolItem,
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

/* Consensus / disagreement — Orallexa PerspectivePanelCard-inspired: one
 * row per specialist with a directional bias badge and its reasoning,
 * plus an agreement summary, rather than a plain bulleted list. */
function ConsensusBlock({ consensus }: { consensus: ConsensusSummary }) {
  const dirColor = (d: string) =>
    d === "bullish" ? "bg-pos" : d === "bearish" ? "bg-neg" : "bg-dim";
  return (
    <Card title="Consensus / disagreement">
      <div className="kv-row">
        <span className="text-dim">Agreement</span>
        <Pill text={consensus.agreement} />
      </div>
      {consensus.signals.length ? (
        <div className="mt-2 flex flex-col gap-1.5">
          {consensus.signals.map((s, i) => (
            <div key={i} className="flex items-start gap-2 text-[0.79rem]">
              <span className={`w-2 h-2 rounded-full mt-1 flex-shrink-0 ${dirColor(s.direction)}`} />
              <div>
                <strong>{s.source}: </strong>
                <Pill text={s.direction} />
                <span> {s.detail}</span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <StateMessage text="No independent signals available to compare." />
      )}
    </Card>
  );
}

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

/* PM -> AI Risk -> execution, rendered as a numbered sequence. The AI
 * Risk verdict card below is deliberately shaped like Orallexa's
 * `PortfolioManagerCard` (approve/reject + reasoning + modifications) —
 * that donor component's fields map to QAMC's risk_manager semantics,
 * not QAMC's own Portfolio Manager (Stage 0 donor-inventory naming-
 * inversion note). */
function DecisionChain({ detail }: { detail: CandidateDetailResponse }) {
  const steps: { title: string; body: JSX.Element }[] = [];

  if (detail.pm_reasoning?.portfolio_view) {
    steps.push({
      title: "Portfolio Manager reasoning",
      body: <CardText text={detail.pm_reasoning.portfolio_view} />,
    });
  }
  if (detail.pm_target) {
    const t = detail.pm_target;
    steps.push({
      title: "Portfolio Manager target",
      body: (
        <>
          <KV label="Target weight" value={`${fmtNum(t.target_weight_pct)}%`} />
          <KV label="Conviction" value={t.conviction} />
          <CardText text={t.thesis} />
        </>
      ),
    });
  }
  if (detail.pm_proposed_order) {
    const p = detail.pm_proposed_order;
    steps.push({
      title: "PM constructed order (pre-review)",
      body: (
        <>
          <div className="kv-row">
            <span className="text-dim">Action</span>
            <Pill text={p.action} />
          </div>
          <KV label="Allocation" value={`${fmtNum(p.allocation_pct)}%`} />
          <KV label="Entry" value={fmtMoney(p.entry_price)} />
          <CardText text={p.reasoning} />
        </>
      ),
    });
  }
  if (detail.risk_verdict) {
    const v = detail.risk_verdict.verdict;
    steps.push({
      title: "AI Risk Manager verdict (run-wide)",
      body: v ? (
        <>
          <div className="kv-row">
            <span className="text-dim">Verdict</span>
            <Pill text={v.approved ? "approved" : "rejected"} />
          </div>
          <CardText text={v.reasoning} />
          {v.modifications.length > 0 && (
            <ul className="mt-1.5 pl-4 text-[0.79rem] list-disc">
              {v.modifications.map((m, i) => (
                <li key={i}>
                  {m.symbol}: {m.field} {m.original_value} &rarr; {m.new_value} ({m.reason})
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <StateMessage text="Verdict recorded but could not be read back." />
      ),
    });
  }
  if (detail.risk_modification) {
    const m = detail.risk_modification;
    steps.push({
      title: "AI Risk Manager modification (this symbol)",
      body: (
        <>
          <KV label="Field" value={m.field} />
          <KV label="Original" value={fmtNum(m.original_value)} />
          <KV label="Modified to" value={fmtNum(m.new_value)} />
          <CardText text={m.reason} />
        </>
      ),
    });
  }
  if (detail.trade) {
    const t = detail.trade;
    steps.push({
      title: "Executed trade",
      body: (
        <>
          <div className="kv-row">
            <span className="text-dim">Action</span>
            <Pill text={t.action} />
          </div>
          <KV label="Qty" value={t.qty !== null && t.qty !== undefined ? fmtNum(t.qty) : null} />
          <KV label="Price" value={fmtMoney(t.price)} />
          <div className="kv-row">
            <span className="text-dim">Fill status</span>
            <Pill text={t.fill_status || "unfilled"} />
          </div>
          {t.reasoning && <CardText text={t.reasoning} />}
          <ProposedVsExecuted proposed={detail.pm_proposed_order} trade={detail.trade} />
        </>
      ),
    });
  } else if (detail.pm_proposed_order) {
    steps.push({
      title: "Executed trade",
      body: (
        <StateMessage
          text={
            detail.risk_verdict?.verdict?.approved === false
              ? "No trade — rejected by the AI Risk Manager before execution."
              : "No trade recorded for this candidate this run (proposed but not executed, or a HOLD)."
          }
        />
      ),
    });
  }

  if (!steps.length) {
    return <StateMessage text="No PM/Risk/execution chain recorded for this candidate this run." />;
  }

  return (
    <div className="flex flex-col gap-3">
      {steps.map((s, i) => (
        <div key={i} className="flex gap-3 items-start">
          <div className="flex-shrink-0 w-6 h-6 rounded-full bg-panel-alt border border-border flex items-center justify-center text-[0.68rem] font-bold text-dim mt-0.5">
            {i + 1}
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-bold text-[0.85rem] mb-1">{s.title}</div>
            {s.body}
          </div>
        </div>
      ))}
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
  const [error, setError] = useState<string | null>(null);
  const { openRunDetail } = useModalActions();

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    api
      .candidateDetail(runId, symbol)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, symbol]);

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
          <EvidenceSection title="Consensus">{[<ConsensusBlock key="c" consensus={detail.consensus} />]}</EvidenceSection>
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
          <EvidenceSection title="Decision chain: PM &rarr; AI Risk &rarr; execution">
            {[<DecisionChain key="chain" detail={detail} />]}
          </EvidenceSection>
        </div>
      )}
    </Modal>
  );
}
