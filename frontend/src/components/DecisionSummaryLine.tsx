import { CandidateDetailResponse, RunFunnelResponse } from "../api/client";
import { fmtNum } from "../lib/format";
import { buildCandidateStages, furthestReachedStage } from "./funnelShared";

/* Compact decision facts for Lifecycle. Built only from real
 * proposed/verdict/gate/execution fields; returns null when nothing has
 * actually happened yet. The strip that used to sit under the chart was
 * removed so the identity row stays one line. */

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
