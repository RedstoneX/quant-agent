import { DecisionState, RunFunnelResponse } from "../api/client";
import { fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card, KV, CardText } from "./ui/Evidence";
import { DecisionFlowDiagram, FlowStage, FlowStatus } from "./DecisionFlowDiagram";

const STATE_LABELS: Record<DecisionState, string> = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

const STATE_COLORS: Record<DecisionState, string> = {
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
function buildFunnelStages(funnel: RunFunnelResponse): FlowStage[] {
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

export function DecisionFunnelPanel({
  funnel,
  loading,
  error,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const status = error ? "error" : loading ? "loading" : "ok";
  return (
    <Panel title="Latest decision — why did it trade, or why not?" status={status} full accent>
      {error && <StateMessage text={`Could not load latest decision: ${error}`} error />}
      {!error && !funnel && <StateMessage text="Loading…" />}
      {funnel && (
        <div>
          <div className="flex items-center gap-3 flex-wrap mb-3">
            <span className={`pill border text-[0.85rem] px-3 py-1 ${STATE_COLORS[funnel.decision_state]}`}>
              {STATE_LABELS[funnel.decision_state]}
            </span>
            <span className="text-dim text-[0.8rem]">
              run {funnel.run_id}
              {funnel.session_prefix ? ` (${funnel.session_prefix})` : ""} &middot; {fmtTime(funnel.timestamp)}
            </span>
          </div>
          <DecisionFlowDiagram stages={buildFunnelStages(funnel)} />
          {funnel.bearish_hedge_considered && (
            <div className="state-message mt-2">
              A bearish inverse-ETF candidate was considered this run — see Candidates below.
            </div>
          )}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5 mt-4">
            <Card title="Market regime" broader>
              {funnel.macro_context ? (
                <>
                  <div className="kv-row">
                    <span className="text-dim">Regime</span>
                    <Pill text={funnel.macro_context.regime} />
                  </div>
                  <KV label="Equity outlook" value={funnel.macro_context.equity_outlook} />
                  <KV label="Confidence" value={funnel.macro_context.confidence} />
                  {funnel.macro_context.summary && <CardText text={funnel.macro_context.summary} />}
                </>
              ) : (
                <StateMessage text="No macro regime evidence recorded for this run." />
              )}
            </Card>
            {funnel.pm_reasoning?.portfolio_view && (
              <Card title="Portfolio Manager">
                <CardText text={funnel.pm_reasoning.portfolio_view} />
              </Card>
            )}
            {funnel.risk_verdict?.verdict && (
              <Card title="AI Risk Manager">
                <div className="kv-row">
                  <span className="text-dim">Verdict</span>
                  <div className="flex gap-1.5 flex-wrap justify-end">
                    <Pill text={funnel.risk_verdict.verdict.approved ? "approved" : "rejected"} />
                    <Pill text={funnel.risk_verdict.verdict.reason_category} />
                  </div>
                </div>
                <CardText text={funnel.risk_verdict.verdict.reasoning} />
              </Card>
            )}
            {funnel.decision_state === "hard_risk_block" && (
              <Card title="Deterministic gate">
                <CardText text="The deterministic hard-risk gate blocked every candidate this run before the AI Risk Manager was ever called." />
              </Card>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
