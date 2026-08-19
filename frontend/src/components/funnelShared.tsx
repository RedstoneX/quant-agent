import { DecisionState, RunFunnelResponse } from "../api/client";
import { fmtNum } from "../lib/format";
import { FlowStage, FlowStatus } from "./DecisionFlowDiagram";

// Shared Specialists -> PM -> AI Risk -> Deterministic Gate -> Execution
// run-level presentation building blocks, used by both the compact
// Decision Room rail (DecisionRoomPanel) and the run detail modal
// (RunDetailModal's FunnelSteps). Split out from the old full-width
// DecisionFunnelPanel once the cockpit redesign replaced that panel with
// the narrower Decision Room.

export const STATE_LABELS: Record<DecisionState, string> = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

export const STATE_COLORS: Record<DecisionState, string> = {
  executed: "bg-pos/15 text-pos border-pos/40",
  proposed_not_executed: "bg-warn/15 text-warn border-warn/40",
  hard_risk_block: "bg-neg/15 text-neg border-neg/40",
  no_proposal: "bg-dim/15 text-dim border-border",
  no_candidates: "bg-dim/15 text-dim border-border",
};

export function FunnelSteps({ funnel }: { funnel: RunFunnelResponse }) {
  const steps: [string, number][] = [
    ["Considered", funnel.candidates_considered],
    ["PM target", funnel.reached_pm_count],
    ["Proposed", funnel.proposed_order_count],
    ["Executed", funnel.executed_count],
  ];
  return (
    <div className="flex items-center gap-1.5 flex-wrap text-[0.78rem]">
      {steps.map(([label, count], i) => (
        <div key={label} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-dim text-base">&rarr;</span>}
          <div className="flex flex-col items-center gap-0.5 min-w-[64px]">
            <div className="text-[1.1rem] font-extrabold tabular-nums">{fmtNum(count, 0)}</div>
            <div className="text-[0.6rem] text-dim uppercase tracking-wide text-center">{label}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// Run-wide aggregate of the same Specialists -> PM -> AI Risk -> gate ->
// execution flow language CandidateDetailModal draws per-candidate — built
// from RunFunnelResponse's own aggregate counts/flags, never a per-candidate
// fabrication. `candidates[].risk_modified`/`executed` are precise
// server-computed booleans (see src/api/routes_evidence.py::get_run_funnel);
// counting them here is exact, not an estimate.
export function buildFunnelStages(funnel: RunFunnelResponse): FlowStage[] {
  const considered = funnel.candidates_considered;
  const pmCount = funnel.reached_pm_count;
  const proposedCount = funnel.proposed_order_count;
  const modifiedCount = funnel.candidates.filter((c) => c.risk_modified).length;
  const execCount = funnel.executed_count;
  const verdict = funnel.risk_verdict?.verdict ?? null;

  let riskStatus: FlowStatus = "not_reached";
  if (funnel.hard_risk_block) riskStatus = "blocked";
  else if (verdict) riskStatus = verdict.approved === false ? "rejected" : modifiedCount > 0 ? "modified" : "approved";
  else if (proposedCount > 0) riskStatus = "pending";

  let gateStatus: FlowStatus = "not_reached";
  let gateCaption: string | undefined;
  if (funnel.hard_risk_block) {
    gateStatus = "blocked";
    gateCaption = "Blocked before AI Risk Manager ran";
  } else if (execCount > 0) {
    gateStatus = "reached";
    gateCaption = `${execCount} cleared`;
  } else if (verdict?.approved === true) {
    gateStatus = "pending";
    gateCaption = "Approved; no execution recorded";
  }

  return [
    { key: "specialists", label: "Specialists", status: considered > 0 ? "reached" : "not_reached", caption: `${fmtNum(considered, 0)} considered` },
    { key: "pm", label: "Portfolio Manager", status: pmCount > 0 ? "reached" : "not_reached", caption: `${fmtNum(pmCount, 0)} reached target` },
    {
      key: "risk",
      label: "AI Risk Manager",
      status: riskStatus,
      caption: proposedCount > 0 ? `${fmtNum(proposedCount, 0)} proposed${modifiedCount ? `, ${fmtNum(modifiedCount, 0)} modified` : ""}` : undefined,
    },
    { key: "gate", label: "Deterministic Gate", status: gateStatus, caption: gateCaption },
    { key: "exec", label: "Execution", status: execCount > 0 ? "executed" : "not_reached", caption: `${fmtNum(execCount, 0)} executed` },
  ];
}
