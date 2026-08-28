import { Badge, Button, Tab, TabGroup, TabList, Text, type Color } from "@tremor/react";
import { RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime, isExecutedTrade } from "../lib/format";
import { STATE_LABELS, isSweepOnlyExecution } from "./funnelShared";

const STATE_BADGE_COLOR: Record<RunFunnelResponse["decision_state"], Color> = {
  executed: "emerald",
  proposed_not_executed: "amber",
  hard_risk_block: "rose",
  no_proposal: "slate",
  no_candidates: "slate",
};

// The original morning pipeline predates named sessions and persists its
// run ids with the generic `run-` prefix. Present that operator-facing
// session by its real meaning instead of making "Morning" look absent.
function sessionLabel(prefix: string | null): string {
  return !prefix || prefix === "run" ? "Morning" : prefix.replace(/_/g, " ");
}

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
  onSelectTrade,
  compact = false,
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
  onSelectTrade: (trade: TradeItem) => void;
  /* Compact chrome (App.tsx's chrome-collapse control): the session tabs
   * stay — picking which session drives the cockpit is the strip's whole
   * job — while the per-session executions table folds away. That table is
   * the single tallest block in the header stack, and the same fills are
   * still reachable in the Trades panel of the workspace below. */
  compact?: boolean;
}) {
  if (runs.length === 0) {
    if (loading) return <Text className="mx-3 mt-3">Loading today&rsquo;s sessions&hellip;</Text>;
    if (error) return <Text className="mx-3 mt-3 text-neg">Could not load today&rsquo;s sessions: {error}</Text>;
    return null;
  }

  const sorted = [...runs].sort((a, b) => (a.first_timestamp || "").localeCompare(b.first_timestamp || ""));
  const activeIndex = Math.max(0, sorted.findIndex((run) => run.run_id === selectedRunId));
  const selectedRun = sorted[activeIndex];
  const executedTrades = trades.filter(
    (trade) => trade.run_id === selectedRun?.run_id && isExecutedTrade(trade)
  );

  return (
    <section className="mx-3 mt-3" aria-label="Today’s sessions">
      <div className="mb-1.5 flex items-center gap-2">
        <Text className="uppercase tracking-wide">Today&rsquo;s sessions</Text>
        {error && <Badge color="amber" size="xs">stale</Badge>}
        {!autoFollow && (
          <Button variant="light" size="xs" color="cyan" onClick={onFollowLatest} className="ml-auto">
            AUTO / PRIMARY
          </Button>
        )}
        {autoFollow && <Badge color="cyan" size="xs" className="ml-auto">AUTO / PRIMARY</Badge>}
      </div>
      <TabGroup index={activeIndex} onIndexChange={(index) => onSelect(sorted[index].run_id)}>
        <TabList variant="solid" color="cyan" className="max-w-full overflow-x-auto rounded-lg bg-panel-alt p-1 ring-1 ring-border">
          {sorted.map((run) => {
            const funnel = funnels[run.run_id];
            return (
              <Tab key={run.run_id} className="gap-2 whitespace-nowrap px-3 py-2">
                <span className="font-semibold uppercase">{sessionLabel(run.session_prefix)}</span>
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
      {!compact && (
      <div className="mt-2 rounded-lg border border-border bg-panel-alt px-3 py-2" aria-label="Selected session trades">
        <div className="mb-1.5 flex items-center gap-2">
          <Text className="font-semibold uppercase tracking-wide">
            {sessionLabel(selectedRun.session_prefix)} executions
          </Text>
          <Badge color={executedTrades.length ? "emerald" : "slate"} size="xs">
            {executedTrades.length} filled
          </Badge>
          <Text className="ml-auto hidden text-xs sm:block">Click a trade to chart it</Text>
        </div>
        {executedTrades.length === 0 ? (
          <Text>No executed trades in this selected session.</Text>
        ) : (
          <div className="overflow-x-auto">
            <div className="min-w-[620px]">
              <div className="grid grid-cols-[1.1fr_.8fr_.8fr_.8fr_1fr_1fr] gap-2 border-b border-border px-2 pb-1 text-[0.65rem] font-semibold uppercase tracking-wide text-dim">
                <span>Symbol</span><span>Side</span><span>Qty</span><span>Fill</span><span>Status</span><span>Time</span>
              </div>
              {executedTrades.map((trade) => {
                const side = trade.action.replace(/^SWEEP_/, "");
                return (
                  <button
                    key={trade.id}
                    type="button"
                    onClick={() => onSelectTrade(trade)}
                    className="grid w-full grid-cols-[1.1fr_.8fr_.8fr_.8fr_1fr_1fr] gap-2 rounded px-2 py-1.5 text-left text-[0.75rem] hover:bg-accent/10 focus:outline-none focus:ring-2 focus:ring-accent/60"
                    aria-label={`Chart ${trade.symbol} ${side} execution`}
                  >
                    <span className="font-bold text-accent">{trade.symbol}</span>
                    <span className={side === "BUY" ? "font-semibold text-pos" : "font-semibold text-neg"}>{side}</span>
                    <span className="font-mono">{fmtNum(trade.fill_qty ?? trade.qty)}</span>
                    <span className="font-mono">{fmtMoney(trade.fill_price ?? trade.price)}</span>
                    <span className="uppercase text-dim">{trade.fill_status || "executed"}</span>
                    <span className="font-mono text-dim">{fmtTime(trade.timestamp)}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
      )}
    </section>
  );
}
