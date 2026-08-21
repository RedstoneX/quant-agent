import { RunSummary } from "../api/client";
import { etDateKey } from "../lib/format";

/* Compact Cockpit run selector (2026-08-21 Mission Control correctness
 * tranche, section B): before this existed, the Cockpit always followed
 * the latest run and had no way to pin an explicit historical run — an
 * operator reviewing the morning run had it silently replaced the moment
 * the next scheduled run (e.g. an intraday tick) landed. LIVE always
 * follows the latest run; any other chip pins the cockpit's Candidates /
 * Decision Room / chart to that one run's own funnel until "Return to
 * Live" is clicked. */

const SESSION_LABELS: Record<string, string> = {
  run: "Morning", // src/pipeline_context.py::RunContext.start: the morning
  // session's run_id prefix is literally "run", not "morning" — every
  // other session type uses its own name as the prefix directly.
  midday: "Midday",
  close: "Close",
  evening: "Evening",
  intra_check: "Intraday",
  earnings_preprocess: "Earnings prep",
};

function sessionLabel(prefix: string | null): string {
  if (!prefix) return "Run";
  if (SESSION_LABELS[prefix]) return SESSION_LABELS[prefix];
  return prefix
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function etTimeOfDay(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** Today's (ET calendar day) runs, oldest-first — a real subset of
 * whatever `runs` the caller already fetched, never a separate/fabricated
 * list. Exported so App.tsx can compute it once and reuse the same set for
 * both the timeline and any "is there a newer run" check. */
export function runsToday(runs: RunSummary[]): RunSummary[] {
  const today = etDateKey(new Date());
  return runs
    .filter((r) => r.first_timestamp && etDateKey(r.first_timestamp) === today)
    .slice()
    .sort((a, b) => (a.first_timestamp || "").localeCompare(b.first_timestamp || ""));
}

export function RunTimeline({
  todayRuns,
  selectedRunId,
  latestRunId,
  onSelect,
  onReturnToLive,
}: {
  /** Today's runs, oldest-first (use `runsToday`). */
  todayRuns: RunSummary[];
  /** `null` means following LIVE (the latest run). */
  selectedRunId: string | null;
  latestRunId: string | null;
  onSelect: (runId: string) => void;
  onReturnToLive: () => void;
}) {
  const isLive = selectedRunId === null;
  const newerRunAvailable = !isLive && latestRunId !== null && latestRunId !== selectedRunId;

  return (
    <div className="flex items-center gap-1.5 flex-wrap px-3 py-1.5" role="tablist" aria-label="Run history">
      <button
        type="button"
        role="tab"
        aria-selected={isLive}
        onClick={onReturnToLive}
        title={isLive ? "Following the latest run" : "Return to Live"}
        className={`relative flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[0.72rem] font-bold uppercase tracking-wide ${
          isLive ? "border-pos bg-pos/15 text-pos" : "border-border bg-panel-alt text-dim hover:border-accent/50 hover:text-ink"
        }`}
      >
        <span className={`w-1.5 h-1.5 rounded-full ${isLive ? "bg-pos animate-pulse" : "bg-dim"}`} />
        LIVE
        {newerRunAvailable && (
          <span
            className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-accent border border-bg"
            title="A newer run has arrived — click to follow it"
          />
        )}
      </button>

      {todayRuns.map((r) => {
        const active = r.run_id === selectedRunId;
        return (
          <button
            key={r.run_id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(r.run_id)}
            title={`${r.run_id} — click to pin the Cockpit to this run`}
            className={`px-2.5 py-1 rounded-md border text-[0.72rem] font-semibold whitespace-nowrap ${
              active ? "border-accent bg-accent/15 text-accent" : "border-border bg-panel-alt text-dim hover:border-accent/50 hover:text-ink"
            }`}
          >
            {sessionLabel(r.session_prefix)} {etTimeOfDay(r.first_timestamp)}
          </button>
        );
      })}

      {!isLive && (
        <span className="text-[0.68rem] text-warn font-semibold uppercase tracking-wide ml-1">
          Pinned — not following new runs
        </span>
      )}
    </div>
  );
}
