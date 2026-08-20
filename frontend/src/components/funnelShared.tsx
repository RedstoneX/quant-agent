import { CandidateFunnelItem, DecisionState, RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtNum } from "../lib/format";
import { FlowStage, FlowStatus } from "./agentflow/types";

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

/* ------------------------------------------------------------------ *
 * Per-candidate furthest-stage bucketing — shared by CandidateRail (the
 * live cockpit rail) and JournalPanel (the day narrative's per-run
 * candidate list), so both surfaces classify and label candidates
 * identically instead of maintaining two copies that could drift.
 * ------------------------------------------------------------------ */

export type Stage = "rejected" | "reached_pm" | "proposed" | "risk_action" | "executed";

export const STAGE_META: Record<Stage, { label: string; dotClass: string; textClass: string; short: string; rank: number }> = {
  executed: { label: "Executed", dotClass: "bg-pos", textClass: "text-pos", short: "executed", rank: 0 },
  risk_action: { label: "Modified / blocked by risk", dotClass: "bg-neg", textClass: "text-neg", short: "risk", rank: 1 },
  proposed: { label: "Proposed", dotClass: "bg-warn", textClass: "text-warn", short: "proposed", rank: 2 },
  reached_pm: { label: "Reached PM", dotClass: "bg-accent", textClass: "text-accent", short: "PM", rank: 3 },
  // Deliberately NOT "Rejected by specialist" (the label this replaced):
  // a candidate in this bucket only failed to reach a PM target. That can
  // mean a specialist flagged it negatively, but just as often means PM
  // passed on portfolio-balance/cash-target/conviction grounds despite a
  // fine specialist read — the data available here cannot tell those
  // apart, so the label must not claim specialist rejection specifically.
  // See docs/WORK.md's "Known dashboard correctness debt" list.
  rejected: { label: "No PM target", dotClass: "bg-dim", textClass: "text-dim", short: "—", rank: 4 },
};

// Furthest-stage first, so the default (unfiltered) ordering surfaces the
// most decision-relevant candidates before the majority that never left
// specialist screening.
export const STAGE_ORDER: Stage[] = ["executed", "risk_action", "proposed", "reached_pm", "rejected"];

// risk_verdict is recorded per run, not per candidate (the AI Risk Manager
// evaluates a run's proposed orders as one batch — see DirectionalBiasPanel's
// identical caveat). A run-wide hard-risk block or rejected verdict is
// therefore attributed to every candidate that reached a proposed order in
// that run; risk_modified is already exact per-candidate.
export function candidateStage(c: CandidateFunnelItem, funnel: RunFunnelResponse): Stage {
  if (c.executed) return "executed";
  const verdict = funnel.risk_verdict?.verdict;
  const riskActed =
    c.risk_modified ||
    (c.reached_proposed_order && funnel.hard_risk_block) ||
    (c.reached_proposed_order && verdict?.approved === false);
  if (riskActed) return "risk_action";
  if (c.reached_proposed_order) return "proposed";
  if (c.reached_pm_target) return "reached_pm";
  return "rejected";
}

// True only when a run's entire recorded trade activity is cash-sweep
// housekeeping (SWEEP_BUY/SWEEP_SELL — deterministic SGOV parking, never a
// Portfolio Manager investment thesis; see docs/architecture/
// MISSION_CONTROL_API.md's Stage 6 liquidity section). A run whose ONLY
// trade is a sweep fill still reports decision_state "executed" from the
// backend (any trade row counts), which reads exactly like a real strategy
// entry unless the UI distinguishes them — this is that distinction, kept
// as a pure display-layer correction over already-fetched trade rows
// rather than a change to `decision_state` itself.
export function isSweepOnlyExecution(trades: TradeItem[]): boolean {
  return trades.length > 0 && trades.every((t) => (t.action || "").startsWith("SWEEP_"));
}

// Which of today's runs should drive the cockpit's primary Candidates /
// Chart / Decision Room view. QAMC runs several session types a day
// (opportunity-scan sessions alongside position-review-only sessions like
// midday/close — see src/pipeline.py's run_midday/run_close, both
// `run_position_review`, structurally distinct from a full specialist scan
// and near-always reporting zero candidates in the funnel sense). Always
// picking the single literal-latest run means a routine afternoon
// position-review silently blanks out a real morning scan's candidates/
// decisions for the rest of the day — the "latest empty runs can erase
// more meaningful session context" debt item in docs/WORK.md, reproduced
// live against real production data while building this.
//
// Deliberately a DATA-driven rule (candidates_considered > 0), not a
// hardcoded session-type allowlist: a hardcoded list of "scanning" prefixes
// would silently misclassify if a session type is ever added/renamed
// upstream, which would be a worse truth bug than the one this fixes. If
// every run today genuinely had zero candidates, this honestly falls back
// to the literal latest run rather than inventing a different answer.
export function bestPrimaryRunId(
  runs: RunSummary[],
  funnels: Record<string, RunFunnelResponse | null | undefined>
): string | null {
  if (!runs.length) return null;
  const sorted = [...runs].sort((a, b) => (b.first_timestamp || "").localeCompare(a.first_timestamp || ""));
  const withCandidates = sorted.find((r) => (funnels[r.run_id]?.candidates_considered ?? 0) > 0);
  return (withCandidates ?? sorted[0]).run_id;
}
