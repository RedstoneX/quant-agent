import { CandidateDetailResponse, CandidateFunnelItem, DecisionState, RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtNum, isExecutedTrade } from "../lib/format";
import { FlowStage, FlowStatus } from "./agentflow/types";

// Shared Specialists -> PM -> AI Risk -> Deterministic Gate -> Execution
// run-level presentation building blocks, used by the run detail modal
// (RunDetailModal's FunnelSteps). Originally also used by the cockpit's
// Decision Room panel; that panel was removed from the cockpit entirely
// in the trader-focused rework (its content either moved inline under the
// chart — see DecisionSummaryLine.tsx/PositionHoldingStrip.tsx — or
// already existed on the Research Desk as DecisionDeltaPanel).

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

/* ------------------------------------------------------------------ *
 * Per-candidate Specialists -> PM -> AI Risk -> Deterministic Gate ->
 * Execution stage derivation. Used by CandidateDetailModal's full
 * drill-down graph, and by DecisionSummaryLine.tsx's one-line cockpit
 * summary (via furthestReachedStage, below) to decide whether there is
 * any real content to report at all for the charted symbol. Every stage's
 * reached/outcome status comes purely from fields CandidateDetailResponse
 * already carries (cross-checked against the run funnel's per-candidate
 * `executed`/`hard_risk_block` when that supplementary fetch succeeds) —
 * never a fabricated guess about a stage with no evidence.
 * ------------------------------------------------------------------ */
export function skipText(reason: string | null, detail: string | null): string | null {
  if (!reason) return null;
  const words = reason.replace(/_/g, " ");
  return detail ? `${words} — ${detail}` : words;
}

export function buildCandidateStages(detail: CandidateDetailResponse, funnel: RunFunnelResponse | null): FlowStage[] {
  const specialistCount = (detail.tech ? 1 : 0) + (detail.earnings ? 1 : 0) + detail.news_symbol.length;
  const specialistsReached = specialistCount > 0;
  const reachedProposedOrder = !!detail.pm_proposed_order;
  const pmReached = !!detail.pm_target || reachedProposedOrder;
  const pmCaption = detail.pm_proposed_order
    ? detail.pm_proposed_order.action
    : detail.pm_target
    ? `PM set a target of ${fmtNum(detail.pm_target.target_weight_pct)}% for this candidate, but no order was constructed. Candidate-specific reason for not constructing an order was not recorded.`
    : undefined;

  // The Risk verdict and hard block are run-scoped. Attribute either to
  // this candidate only when it actually reached a symbol-specific order.
  const verdict = reachedProposedOrder ? detail.risk_verdict?.verdict ?? null : null;
  const hardBlocked = reachedProposedOrder && funnel?.hard_risk_block === true;
  let riskStatus: FlowStatus = "not_reached";
  let riskCaption: string | undefined;
  if (verdict) {
    const hasMods = !!detail.risk_modification || verdict.modifications.length > 0;
    riskStatus = verdict.approved === false ? "rejected" : hasMods ? "modified" : "approved";
    riskCaption = verdict.reason_category ? verdict.reason_category.replace(/_/g, " ") : undefined;
  } else if (hardBlocked) {
    riskStatus = "blocked";
    riskCaption = "Deterministic hard-risk gate blocked this run before the AI Risk Manager was called.";
  }

  const funnelCandidate = funnel?.candidates.find((c) => c.symbol === detail.symbol) ?? null;
  const executed = funnelCandidate ? funnelCandidate.executed : !!detail.trade && isExecutedTrade(detail.trade);
  const skip = funnelCandidate ? skipText(funnelCandidate.execution_skip_reason, funnelCandidate.execution_skip_detail) : null;

  let gateStatus: FlowStatus = "not_reached";
  let gateCaption: string | undefined;
  if (hardBlocked) {
    gateStatus = "blocked";
    gateCaption = "Hard-risk gate blocked every candidate this run before the AI Risk Manager was called.";
  } else if (detail.trade) {
    gateStatus = "reached";
    gateCaption = "Cleared for execution.";
  } else if (skip) {
    gateStatus = "blocked";
    gateCaption = `Deterministic gate skipped this order — ${skip}.`;
  } else if (verdict?.approved === true) {
    gateStatus = "pending";
    gateCaption = "Approved upstream; no trade recorded and no execution-skip reason was persisted for this candidate.";
  } else if (verdict?.approved === false) {
    gateCaption = "Rejected upstream by the AI Risk Manager — never reached the deterministic gate.";
  } else if (reachedProposedOrder) {
    gateCaption = "Not reached — no usable AI Risk Manager verdict recorded for this run.";
  }

  let execStatus: FlowStatus = "not_reached";
  let execCaption: string | undefined;
  if (executed) {
    execStatus = "executed";
    execCaption = detail.trade
      ? `${detail.trade.action}${detail.trade.qty !== null && detail.trade.qty !== undefined ? ` ${fmtNum(detail.trade.qty)}sh` : ""}`
      : undefined;
  } else if (skip) {
    execStatus = "blocked";
    execCaption = `Execution skipped — ${skip}.`;
  } else if (verdict?.approved === false) {
    execCaption = "No trade — rejected by the AI Risk Manager before execution.";
  } else if (reachedProposedOrder) {
    execCaption = "Proposed but not executed this run (or a HOLD). Execution reason was not recorded.";
  }

  return [
    {
      key: "specialists",
      label: "Specialists",
      status: specialistsReached ? "reached" : "not_reached",
      caption: !specialistsReached
        ? "No evidence recorded"
        : pmReached
        ? `${specialistCount} signal${specialistCount === 1 ? "" : "s"}`
        : `${specialistCount} signal${specialistCount === 1 ? "" : "s"} recorded — the Portfolio Manager did not select this candidate; a candidate-specific reason was not recorded.`,
    },
    { key: "pm", label: "Portfolio Manager", status: pmReached ? "reached" : "not_reached", caption: pmCaption },
    { key: "risk", label: "AI Risk Manager", status: riskStatus, caption: riskCaption },
    { key: "gate", label: "Deterministic Gate", status: gateStatus, caption: gateCaption },
    { key: "exec", label: "Execution", status: execStatus, caption: execCaption },
  ];
}

// The last stage (in pipeline order) that actually has status !==
// "not_reached" — used both by CandidateDetailModal's OutcomeBanner
// ("Stopped at: …") and by DecisionSummaryLine.tsx to decide whether
// there is any real content to show on the cockpit at all. A funnel/
// candidate where every stage is "not_reached" returns null; callers use
// that to render nothing at all — never a column of "Not reached"
// placeholders (item 5 of the cockpit trader rework; the owner later
// escalated this to "delete the panel entirely," see PR description).
export function furthestReachedStage(stages: FlowStage[]): FlowStage | null {
  for (let i = stages.length - 1; i >= 0; i--) {
    if (stages[i].status !== "not_reached") return stages[i];
  }
  return null;
}
