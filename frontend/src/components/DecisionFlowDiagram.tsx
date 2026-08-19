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

export function DecisionFlowDiagram({ stages }: { stages: FlowStage[] }): ReactNode {
  return (
    <div className="flex items-stretch gap-0 overflow-x-auto pb-1 -mx-0.5 px-0.5">
      {stages.map((s, i) => {
        const style = STATUS_STYLE[s.status];
        return (
          <div key={s.key} className="flex items-center flex-shrink-0">
            {i > 0 && (
              <div
                className={`w-5 sm:w-8 h-0 border-t-2 border-dashed flex-shrink-0 mx-0.5 sm:mx-1 ${connectorTone(s.status)}`}
                aria-hidden="true"
              />
            )}
            <div className={`min-w-[104px] sm:min-w-[132px] rounded-lg border px-2.5 py-2 ${style.border} ${style.bg}`}>
              <div className="flex items-center gap-1.5 mb-0.5">
                <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${style.dot}`} aria-hidden="true" />
                <span className="font-bold text-[0.72rem] leading-tight">{s.label}</span>
              </div>
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
