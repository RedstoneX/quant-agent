import { useEffect, useState } from "react";
import { Badge } from "@tremor/react";
import { api, CandidateDetailResponse, RunFunnelResponse } from "../api/client";
import { fmtNum } from "../lib/format";
import { buildCandidateStages, furthestReachedStage } from "./funnelShared";

/* Owner correction: the Decision Room panel is gone from the cockpit
 * entirely (its specialists -> PM -> AI Risk -> deterministic gate ->
 * execution chain is genuinely useful but RUN-scoped, not
 * position-scoped, and the Research Desk's existing "Read / PM / Risk"
 * panel — DecisionDeltaPanel, research/ResearchPanels.tsx — already covers
 * that same chain for a selected run with standard components; see the
 * PR description for why the cockpit copy was deleted rather than moved).
 * The ONLY trace that may remain on the cockpit is this: one compact line
 * for whichever symbol is charted, built only from real proposed/verdict/
 * gate/execution facts, and rendered as nothing at all — not a column of
 * "Not reached" placeholders — when nothing has actually happened yet. */

export function summarizeDecision(detail: CandidateDetailResponse, funnel: RunFunnelResponse): string | null {
  const stages = buildCandidateStages(detail, funnel);
  if (!furthestReachedStage(stages)) return null;

  const parts: string[] = [];
  if (detail.pm_proposed_order) {
    parts.push(`PM proposed ${detail.pm_proposed_order.action} ${fmtNum(detail.pm_proposed_order.allocation_pct)}%`);
  } else if (detail.pm_target) {
    parts.push(`PM target ${fmtNum(detail.pm_target.target_weight_pct)}%`);
  }
  const verdict = detail.risk_verdict?.verdict;
  if (verdict) {
    if (verdict.approved === false) parts.push("Risk rejected");
    else if (detail.risk_modification) {
      const field = detail.risk_modification.field ? detail.risk_modification.field.replace(/_/g, " ") : "value";
      parts.push(`Risk cut ${field} to ${fmtNum(detail.risk_modification.new_value)}`);
    } else parts.push("Risk approved");
  }
  const gate = stages.find((s) => s.key === "gate");
  if (gate && gate.status !== "not_reached") {
    parts.push(gate.status === "blocked" ? "gate blocked" : "gate allowed");
  }
  if (detail.trade) parts.push(`executed ${detail.trade.action}`);

  return parts.length ? parts.join(", ") : null;
}

// No drill-down link here on purpose: ChartPane/SelectedSymbolContext
// already render an explicit "Lifecycle"/"Full drill-down" button right
// next to the chart for the same candidate — this line stays purely
// informational rather than duplicating that control.
export function DecisionSummaryLine({ funnel, symbol }: { funnel: RunFunnelResponse | null; symbol: string | null }) {
  const candidateEligible = !!(funnel && symbol && funnel.candidates.some((c) => c.symbol === symbol));
  const [detail, setDetail] = useState<CandidateDetailResponse | null>(null);
  const [detailKey, setDetailKey] = useState<string | null>(null);

  useEffect(() => {
    if (!candidateEligible || !funnel || !symbol) {
      setDetail(null);
      setDetailKey(null);
      return;
    }
    let cancelled = false;
    api
      .candidateDetail(funnel.run_id, symbol)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setDetailKey(`${funnel.run_id}:${symbol}`);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [candidateEligible, funnel?.run_id, symbol]);

  const ready = candidateEligible && detail && funnel && symbol && detailKey === `${funnel.run_id}:${symbol}`;
  if (!ready || !detail || !funnel || !symbol) return null;

  const text = summarizeDecision(detail, funnel);
  if (!text) return null;

  return (
    <div className="flex items-center gap-2 rounded-lg border border-border bg-panel-alt px-3 py-1.5 text-[length:var(--fs-meta)]">
      <Badge color="cyan" size="xs" className="flex-shrink-0">Decision</Badge>
      <span className="min-w-0 flex-1 text-ink break-words">{text}</span>
    </div>
  );
}
