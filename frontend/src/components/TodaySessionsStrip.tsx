import { Badge, Button, Tab, TabGroup, TabList, Text, type Color } from "@tremor/react";
import { RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtTime } from "../lib/format";
import { STATE_LABELS, isSweepOnlyExecution } from "./funnelShared";

const STATE_BADGE_COLOR: Record<RunFunnelResponse["decision_state"], Color> = {
  executed: "emerald",
  proposed_not_executed: "amber",
  hard_risk_block: "rose",
  no_proposal: "slate",
  no_candidates: "slate",
};

function SessionBadge({ funnel, runTrades }: { funnel: RunFunnelResponse; runTrades: TradeItem[] }) {
  const sweepOnly = funnel.decision_state === "executed" && isSweepOnlyExecution(runTrades);
  const text = sweepOnly
    ? "sweep only"
    : funnel.candidates_considered > 0
      ? `${funnel.candidates_considered} candidate${funnel.candidates_considered === 1 ? "" : "s"}`
      : STATE_LABELS[funnel.decision_state];
  return (
    <Badge color={sweepOnly ? "slate" : STATE_BADGE_COLOR[funnel.decision_state]} size="xs">
      {text}
    </Badge>
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
    if (loading) return <Text className="mx-3 mt-3">Loading today&rsquo;s sessions&hellip;</Text>;
    if (error) return <Text className="mx-3 mt-3 text-neg">Could not load today&rsquo;s sessions: {error}</Text>;
    return null;
  }

  const sorted = [...runs].sort((a, b) => (a.first_timestamp || "").localeCompare(b.first_timestamp || ""));
  const activeIndex = Math.max(0, sorted.findIndex((run) => run.run_id === selectedRunId));

  return (
    <section className="mx-3 mt-3" aria-label="Today’s sessions">
      <div className="mb-1.5 flex items-center gap-2">
        <Text className="uppercase tracking-wide">Today&rsquo;s sessions</Text>
        {error && <Badge color="amber" size="xs">stale</Badge>}
        {!autoFollow && (
          <Button variant="light" size="xs" color="cyan" onClick={onFollowLatest} className="ml-auto">
            Follow latest
          </Button>
        )}
      </div>
      <TabGroup index={activeIndex} onIndexChange={(index) => onSelect(sorted[index].run_id)}>
        <TabList variant="solid" color="cyan" className="max-w-full overflow-x-auto rounded-lg bg-panel-alt p-1 ring-1 ring-border">
          {sorted.map((run) => {
            const funnel = funnels[run.run_id];
            return (
              <Tab key={run.run_id} className="gap-2 whitespace-nowrap px-3 py-2">
                <span className="font-semibold uppercase">{run.session_prefix || "run"}</span>
                <span className="font-mono text-dim">{fmtTime(run.first_timestamp)}</span>
                {funnel ? (
                  <SessionBadge funnel={funnel} runTrades={trades.filter((trade) => trade.run_id === run.run_id)} />
                ) : (
                  <span className="text-dim">&hellip;</span>
                )}
              </Tab>
            );
          })}
        </TabList>
      </TabGroup>
    </section>
  );
}
