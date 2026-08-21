import { Badge, Callout, type Color } from "@tremor/react";
import { RunFunnelResponse, TradeItem } from "../api/client";
import { fmtTime } from "../lib/format";
import { STATE_LABELS, isSweepOnlyExecution } from "./funnelShared";

/* Full-width "why did it trade, or why not" verdict — promoted out of the
 * Decision Room's right column per the operator-approved Concept C. A
 * generic admin panel shows metrics; this banner renders a JUDGMENT, which
 * is the single strongest signal this is a real Mission Control and not a
 * dashboard template. Also the natural home for the stale-data
 * truthfulness fix: a failed poll never lets an old EXECUTED/REJECTED
 * verdict read as current without an explicit STALE treatment. */

const STATE_TONE: Record<RunFunnelResponse["decision_state"], Color> = {
  executed: "emerald",
  proposed_not_executed: "amber",
  hard_risk_block: "rose",
  no_proposal: "slate",
  no_candidates: "slate",
};

function whySummary(funnel: RunFunnelResponse, sweepOnly: boolean): string {
  switch (funnel.decision_state) {
    case "executed": {
      if (sweepOnly) {
        return "Only cash-sweep housekeeping (SGOV parking/unparking) filled this run — no strategy trade.";
      }
      const n = funnel.executed_count;
      const rest = funnel.candidates.filter((c) => !c.executed && c.reached_proposed_order).length;
      return `${n} order${n === 1 ? "" : "s"} executed${rest ? `, ${rest} more proposed but not executed this run` : ""}.`;
    }
    case "proposed_not_executed": {
      const verdict = funnel.risk_verdict?.verdict;
      if (verdict?.approved === false) {
        return `AI Risk Manager rejected the proposed order(s) — ${verdict.reason_category.replace(/_/g, " ")}.`;
      }
      return `${funnel.proposed_order_count} order(s) proposed this run but not executed.`;
    }
    case "hard_risk_block":
      return "The deterministic risk gate blocked every candidate before the AI Risk Manager was ever called.";
    case "no_proposal":
      return `${funnel.reached_pm_count} candidate(s) reached the Portfolio Manager, but it proposed no order — deliberate neutral stance, cash preserved.`;
    case "no_candidates":
    default:
      return "No candidates were considered this run.";
  }
}

export function DecisionStateBanner({
  funnel,
  trades,
  loading,
  error,
  updatedAt,
}: {
  funnel: RunFunnelResponse | null;
  /** Recent trades (any run) — filtered here to this funnel's run_id, same
   * pattern JournalPanel's RunNarrativeCard already uses, so a run whose
   * only fill was cash-sweep housekeeping doesn't read as a strategy
   * EXECUTED. */
  trades: TradeItem[];
  loading: boolean;
  error: string | null;
  updatedAt: Date | null;
}) {
  if (!funnel) {
    if (loading) return null;
    if (!error) {
      // Honest empty state, not a failure: before today's first session
      // runs (or on a non-trading day) there is truthfully nothing to
      // report yet — see docs/OUTCOME.md's "empty/no-trade states should
      // look intentional" principle.
      return (
        <Callout title="No decision yet" color="slate" className="mx-3 mt-3 !bg-panel-alt !ring-border">
          No sessions recorded yet today.
        </Callout>
      );
    }
    return (
      <Callout title="Decision unavailable" color="rose" className="mx-3 mt-3 !bg-panel-alt">
        Could not load the latest decision: {error}
      </Callout>
    );
  }

  const sweepOnly =
    funnel.decision_state === "executed" &&
    isSweepOnlyExecution(trades.filter((t) => t.run_id === funnel.run_id));
  const tone: Color = sweepOnly ? "slate" : STATE_TONE[funnel.decision_state];
  const label = sweepOnly ? "CASH SWEEP ONLY" : STATE_LABELS[funnel.decision_state];
  const stale = Boolean(error);

  return (
    <Callout title={label} color={tone} className="mx-3 mt-3 !bg-panel-alt !ring-border">
      {stale && (
        <div className="mb-1.5 flex items-center gap-2 text-warn text-xs font-semibold">
          <Badge color="amber" size="xs">stale</Badge>
          Stale — last known data{updatedAt ? ` as of ${updatedAt.toLocaleTimeString()}` : ""}, fresh fetch failed ({error})
        </div>
      )}
      <span className="font-mono text-xs text-dim">
        run {funnel.run_id}
        {funnel.session_prefix ? ` · ${funnel.session_prefix}` : ""} · {fmtTime(funnel.timestamp)}
      </span>
      <span className="mt-1 block text-sm leading-snug text-ink">{whySummary(funnel, sweepOnly)}</span>
    </Callout>
  );
}
