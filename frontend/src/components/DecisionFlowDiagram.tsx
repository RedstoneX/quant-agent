import { ReactNode } from "react";

/* Reusable Specialists -> Portfolio Manager -> AI Risk Manager ->
 * Deterministic Gate -> Execution flow visualization. Purely
 * presentational: callers (CandidateDetailModal for a single candidate,
 * DecisionFunnelPanel for a run-wide aggregate) build the `FlowStage[]`
 * from whichever response type they have — this component never reaches
 * into CandidateDetailResponse/RunFunnelResponse itself, so it stays
 * reusable across both "one candidate reached/outcome" and "N candidates
 * this stage" shapes without inventing a shared schema neither backend
 * type actually has. */

export type FlowStatus =
  | "reached"
  | "not_reached"
  | "approved"
  | "modified"
  | "rejected"
  | "blocked"
  | "executed"
  | "pending";

export interface FlowStage {
  key: string;
  label: string;
  status: FlowStatus;
  /** short line under the label — a count, an action, a one-word outcome. Never fabricated: omit when there's nothing honest to show. */
  caption?: string;
}

const STATUS_STYLE: Record<FlowStatus, { border: string; bg: string; text: string; dot: string }> = {
  reached: { border: "border-pos/50", bg: "bg-pos/10", text: "text-pos", dot: "bg-pos" },
  executed: { border: "border-pos/50", bg: "bg-pos/10", text: "text-pos", dot: "bg-pos" },
  approved: { border: "border-pos/50", bg: "bg-pos/10", text: "text-pos", dot: "bg-pos" },
  modified: { border: "border-warn/50", bg: "bg-warn/10", text: "text-warn", dot: "bg-warn" },
  pending: { border: "border-warn/50", bg: "bg-warn/10", text: "text-warn", dot: "bg-warn" },
  rejected: { border: "border-neg/50", bg: "bg-neg/10", text: "text-neg", dot: "bg-neg" },
  blocked: { border: "border-neg/50", bg: "bg-neg/10", text: "text-neg", dot: "bg-neg" },
  not_reached: { border: "border-border", bg: "bg-panel-alt", text: "text-dim", dot: "bg-dim" },
};

const STATUS_TEXT: Record<FlowStatus, string> = {
  reached: "Reached",
  executed: "Executed",
  approved: "Approved",
  modified: "Modified",
  pending: "Pending",
  rejected: "Rejected",
  blocked: "Blocked",
  not_reached: "Not reached",
};

// Connector color reflects the status of the node it leads INTO — i.e.
// whether the process actually arrived at that stage, and how.
function connectorTone(status: FlowStatus): string {
  if (status === "not_reached") return "border-border";
  if (status === "rejected" || status === "blocked") return "border-neg/50";
  if (status === "modified" || status === "pending") return "border-warn/50";
  return "border-pos/50";
}

export function DecisionFlowDiagram({
  stages,
  layout = "horizontal",
}: {
  stages: FlowStage[];
  /** "vertical" stacks the chain top-to-bottom so all 5 stages are visible
   * without horizontal scrolling — needed for narrow contexts (the
   * cockpit's ~340px Decision Room rail) where the horizontal layout's 5
   * nodes don't fit and the tail (deterministic gate, execution — exactly
   * the stages a "why did/didn't it trade" glance most needs) end up
   * hidden behind a scrollbar. */
  layout?: "horizontal" | "vertical";
}): ReactNode {
  if (layout === "vertical") {
    return (
      <div className="flex flex-col">
        {stages.map((s, i) => {
          const style = STATUS_STYLE[s.status];
          const isGate = s.key === "gate";
          return (
            <div key={s.key}>
              {i > 0 && <div className={`w-0 h-2.5 ml-[13px] border-l-2 border-dashed ${connectorTone(s.status)}`} aria-hidden="true" />}
              <div
                className={`flex items-start gap-2 rounded-lg px-2.5 py-1.5 ${isGate ? "border-2" : "border"} ${style.border} ${style.bg}`}
              >
                <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 mt-1.5 ${style.dot}`} aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="font-bold text-[0.76rem] leading-tight">{s.label}</span>
                    <span className={`text-[0.62rem] font-semibold uppercase tracking-wide ${style.text}`}>{STATUS_TEXT[s.status]}</span>
                  </div>
                  {isGate && <div className="text-[0.56rem] text-dim uppercase tracking-wide font-semibold">Final authority</div>}
                  {s.caption && <div className="text-[0.7rem] text-dim mt-0.5 leading-snug">{s.caption}</div>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex items-stretch gap-0 overflow-x-auto pb-1 -mx-0.5 px-0.5">
      {stages.map((s, i) => {
        const style = STATUS_STYLE[s.status];
        // The deterministic gate is the final, non-negotiable safety
        // authority — every model-driven stage before it is advisory by
        // comparison. Visually distinguished (thicker border + a small
        // "final authority" marker) so it doesn't read as just one more
        // agent in the chain, per the project's decision-chain contract:
        // Specialists -> PM -> AI Risk -> deterministic gate -> execution.
        const isGate = s.key === "gate";
        return (
          <div key={s.key} className="flex items-center flex-shrink-0">
            {i > 0 && (
              <div
                className={`w-5 sm:w-8 h-0 border-t-2 border-dashed flex-shrink-0 mx-0.5 sm:mx-1 ${connectorTone(s.status)}`}
                aria-hidden="true"
              />
            )}
            <div
              className={`min-w-[104px] sm:min-w-[132px] rounded-lg px-2.5 py-2 ${
                isGate ? "border-2" : "border"
              } ${style.border} ${style.bg} ${isGate ? "ring-1 ring-offset-0 ring-ink/10" : ""}`}
            >
              <div className="flex items-center gap-1.5 mb-0.5">
                <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${style.dot}`} aria-hidden="true" />
                <span className="font-bold text-[0.72rem] leading-tight">{s.label}</span>
              </div>
              {isGate && (
                <div className="text-[0.56rem] text-dim uppercase tracking-wide font-semibold mb-0.5">
                  Final authority
                </div>
              )}
              <div className={`text-[0.64rem] font-semibold uppercase tracking-wide ${style.text}`}>
                {STATUS_TEXT[s.status]}
              </div>
              {s.caption && <div className="text-[0.68rem] text-dim mt-0.5 leading-snug">{s.caption}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
