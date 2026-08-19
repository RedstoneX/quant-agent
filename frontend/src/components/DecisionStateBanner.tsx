import { RunFunnelResponse } from "../api/client";
import { fmtTime } from "../lib/format";
import { STATE_LABELS } from "./funnelShared";

/* Full-width "why did it trade, or why not" verdict — promoted out of the
 * Decision Room's right column per the operator-approved Concept C. A
 * generic admin panel shows metrics; this banner renders a JUDGMENT, which
 * is the single strongest signal this is a real Mission Control and not a
 * dashboard template. Also the natural home for the stale-data
 * truthfulness fix: a failed poll never lets an old EXECUTED/REJECTED
 * verdict read as current without an explicit STALE treatment. */

const STATE_TONE: Record<
  RunFunnelResponse["decision_state"],
  { border: string; bg: string; text: string; icon: string }
> = {
  executed: { border: "border-pos/50", bg: "bg-pos/8", text: "text-pos", icon: "●" },
  proposed_not_executed: { border: "border-warn/50", bg: "bg-warn/8", text: "text-warn", icon: "◐" },
  hard_risk_block: { border: "border-neg/50", bg: "bg-neg/8", text: "text-neg", icon: "■" },
  no_proposal: { border: "border-border-strong", bg: "bg-panel-alt", text: "text-dim", icon: "○" },
  no_candidates: { border: "border-border-strong", bg: "bg-panel-alt", text: "text-dim", icon: "○" },
};

function whySummary(funnel: RunFunnelResponse): string {
  switch (funnel.decision_state) {
    case "executed": {
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
  loading,
  error,
  updatedAt,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  updatedAt: Date | null;
}) {
  if (!funnel) {
    if (loading) return null;
    return (
      <div className="mx-3 mt-3 rounded-xl border border-neg/40 bg-neg/8 px-4 py-3 text-neg text-[0.85rem]">
        Could not load the latest decision: {error || "no data"}
      </div>
    );
  }

  const tone = STATE_TONE[funnel.decision_state];
  const stale = Boolean(error);

  return (
    <div className={`mx-3 mt-3 rounded-xl border-2 ${tone.border} ${tone.bg} px-4 py-3`}>
      {stale && (
        <div className="mb-1.5 flex items-center gap-1.5 text-warn text-[0.68rem] font-bold uppercase tracking-wide">
          <span className="w-1.5 h-1.5 rounded-full bg-warn animate-pulse" />
          Stale — last known data{updatedAt ? ` as of ${updatedAt.toLocaleTimeString()}` : ""}, fresh fetch failed ({error})
        </div>
      )}
      <div className="flex items-center gap-3 flex-wrap">
        <span className={`text-[1.15rem] font-extrabold tracking-tight ${tone.text}`}>
          {tone.icon} {STATE_LABELS[funnel.decision_state]}
        </span>
        <span className="text-dim text-[0.72rem] font-mono num">
          run {funnel.run_id}
          {funnel.session_prefix ? ` · ${funnel.session_prefix}` : ""} · {fmtTime(funnel.timestamp)}
        </span>
      </div>
      <p className="text-[0.85rem] mt-1 leading-snug">{whySummary(funnel)}</p>
    </div>
  );
}
