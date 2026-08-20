import { RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtTime } from "../lib/format";
import { STATE_LABELS, STATE_COLORS, isSweepOnlyExecution } from "./funnelShared";

/* Compact same-day session picker, sitting above the primary Candidates /
 * Chart / Decision Room workstation. QAMC runs several session types a
 * day — opportunity-scan sessions alongside position-review-only sessions
 * (midday/close) that structurally carry zero candidates — and the
 * workstation below can only show ONE run's funnel at a time. Without
 * this strip, whichever run happens to be literal-latest (often an
 * afternoon position review) silently stands in for "today," erasing a
 * real morning scan's candidates/decisions from view. This strip keeps
 * every real session for the day visible and selectable, so nothing that
 * actually happened is hidden — see funnelShared.ts::bestPrimaryRunId for
 * the default-selection half of this fix. */

function ChipLabel({ funnel, runTrades }: { funnel: RunFunnelResponse; runTrades: TradeItem[] }) {
  const sweepOnly = funnel.decision_state === "executed" && isSweepOnlyExecution(runTrades);
  if (sweepOnly) {
    return <span className="px-1.5 py-0.5 rounded text-[0.62rem] font-bold border bg-dim/15 text-dim border-border">sweep only</span>;
  }
  const text = funnel.candidates_considered > 0 ? `${funnel.candidates_considered} candidate${funnel.candidates_considered === 1 ? "" : "s"}` : STATE_LABELS[funnel.decision_state];
  return (
    <span className={`px-1.5 py-0.5 rounded text-[0.62rem] font-bold border ${STATE_COLORS[funnel.decision_state]}`}>
      {text}
    </span>
  );
}

export function TodaySessionsStrip({
  runs,
  funnels,
  trades,
  loading,
  error,
  selectedRunId,
  autoFollow,
  onSelect,
  onFollowLatest,
}: {
  runs: RunSummary[];
  funnels: Record<string, RunFunnelResponse | null | undefined>;
  trades: TradeItem[];
  loading: boolean;
  error: string | null;
  selectedRunId: string | null;
  autoFollow: boolean;
  onSelect: (runId: string) => void;
  onFollowLatest: () => void;
}) {
  if (runs.length === 0) {
    if (loading) return <div className="mx-3 mt-3 text-[0.72rem] text-dim">Loading today&rsquo;s sessions&hellip;</div>;
    if (error) return <div className="mx-3 mt-3 text-[0.72rem] text-neg">Could not load today&rsquo;s sessions: {error}</div>;
    return null;
  }

  const sorted = [...runs].sort((a, b) => (a.first_timestamp || "").localeCompare(b.first_timestamp || ""));

  return (
    <div className="mx-3 mt-3 flex items-center gap-2 overflow-x-auto pb-1">
      <span className="text-[0.62rem] text-dim uppercase tracking-wide font-semibold flex-shrink-0">Today&rsquo;s sessions</span>
      {sorted.map((r) => {
        const f = funnels[r.run_id];
        const active = r.run_id === selectedRunId;
        return (
          <button
            key={r.run_id}
            type="button"
            onClick={() => onSelect(r.run_id)}
            className={`flex-shrink-0 flex items-center gap-1.5 px-2 py-1 rounded-md border text-[0.72rem] ${
              active ? "border-accent bg-accent/10" : "border-border bg-panel-alt hover:border-accent/50"
            }`}
          >
            <span className="font-semibold uppercase">{r.session_prefix || "run"}</span>
            <span className="text-dim font-mono num">{fmtTime(r.first_timestamp)}</span>
            {f ? (
              <ChipLabel funnel={f} runTrades={trades.filter((t) => t.run_id === r.run_id)} />
            ) : (
              <span className="text-dim">&hellip;</span>
            )}
          </button>
        );
      })}
      {!autoFollow && (
        <button type="button" onClick={onFollowLatest} className="flex-shrink-0 text-accent underline text-[0.7rem] ml-1">
          Follow latest
        </button>
      )}
      {error && <span className="flex-shrink-0 text-warn text-[0.68rem]">stale ({error})</span>}
    </div>
  );
}
