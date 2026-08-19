/* Shared stage-status vocabulary for the Specialists -> PM -> AI Risk ->
 * Deterministic Gate -> Execution chain — consumed by both funnelShared.tsx
 * (run-level aggregate derivation) and buildGraph.ts (the React Flow node
 * builders). Previously lived in the now-retired DecisionFlowDiagram.tsx. */
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
