import { useState } from "react";
import { Badge, Button, Tab, TabGroup, TabList, Text, type Color } from "@tremor/react";
import { RunFunnelResponse, RunSummary, TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime, isExecutedTrade } from "../lib/format";
import { STATE_LABELS, isSweepOnlyExecution } from "./funnelShared";

// Item 12 (cockpit trader rework): green/red is reserved exclusively for
// money direction (P&L). A session chip's state is workflow status, not a
// gain/loss fact, so this uses cyan (the app's own accent/"notable" tone)
// and amber (already the app's caution tone) instead of emerald/rose.
const STATE_BADGE_COLOR: Record<RunFunnelResponse["decision_state"], Color> = {
  executed: "cyan",
  proposed_not_executed: "amber",
  hard_risk_block: "amber",
  no_proposal: "slate",
  no_candidates: "slate",
};

// The original morning pipeline predates named sessions and persists its
// run ids with the generic `run-` prefix. Present that operator-facing
// session by its real meaning instead of making "Morning" look absent.
function sessionLabel(prefix: string | null): string {
  return !prefix || prefix === "run" ? "Morning" : prefix.replace(/_/g, " ");
}

function shortTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (isNaN(d.getTime())) return null;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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

/** True when a run produced no real strategy trade — either it never
 * reached execution, or its only fill was cash-sweep housekeeping. Shared
 * by the one-line summary's "N no-trade" count and nothing else; kept
 * local since it's a display-layer rollup, not a funnel-derivation
 * primitive. */
function isNoTradeRun(funnel: RunFunnelResponse | null | undefined, runTrades: TradeItem[]): boolean {
  if (!funnel) return false;
  if (funnel.decision_state !== "executed") return true;
  return isSweepOnlyExecution(runTrades);
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
}) {
  // Item 8 (cockpit trader rework): eleven chips with text truncated
  // mid-word was a job log, not trading information, permanently occupying
  // a band of the primary screen. Collapsed to one summary line by
  // default; the full session tab strip + selected-session executions
  // table (identical to what this component has always rendered) is one
  // click away, not gone. Local state, not the header's global compact
  // toggle — this needs to default collapsed regardless of that setting.
  const [expanded, setExpanded] = useState(false);

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

  const lastRun = sorted[sorted.length - 1];
  const lastTime = shortTime(lastRun?.first_timestamp);
  const noTradeCount = sorted.filter((run) => isNoTradeRun(funnels[run.run_id], trades.filter((t) => t.run_id === run.run_id))).length;
  const fillsCount = trades.filter((t) => isExecutedTrade(t) && !(t.action || "").startsWith("SWEEP_")).length;
  const summaryText = `${sorted.length} session${sorted.length === 1 ? "" : "s"}${
    lastTime ? ` · last ${lastTime}` : ""
  } · ${noTradeCount} no-trade · ${fillsCount} fill${fillsCount === 1 ? "" : "s"}`;

  return (
    <section className="mx-3 mt-3" aria-label="Today’s sessions">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 rounded-lg border border-border bg-panel px-3 py-1.5 text-left hover:border-accent/60"
      >
        <Text className="uppercase tracking-wide flex-shrink-0">Sessions</Text>
        <span className="text-[length:var(--fs-meta)] text-ink truncate">{summaryText}</span>
        {error && <Badge color="amber" size="xs">stale</Badge>}
        {autoFollow && <Badge color="cyan" size="xs" className="ml-auto flex-shrink-0">AUTO / PRIMARY</Badge>}
        <span className="text-dim text-xs flex-shrink-0" aria-hidden="true">{expanded ? "▾ hide" : "▸ show"}</span>
      </button>

      {expanded && (
        <div className="mt-2">
          <div className="mb-1.5 flex items-center gap-2">
            {!autoFollow && (
              <Button variant="light" size="xs" color="cyan" onClick={onFollowLatest} className="ml-auto">
                AUTO / PRIMARY
              </Button>
            )}
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
          <div className="mt-2 rounded-lg border border-border bg-panel-alt px-3 py-2" aria-label="Selected session trades">
            <div className="mb-1.5 flex items-center gap-2">
              <Text className="font-semibold uppercase tracking-wide">
                {sessionLabel(selectedRun.session_prefix)} executions
              </Text>
              <Badge color={executedTrades.length ? "cyan" : "slate"} size="xs">
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
        </div>
      )}
    </section>
  );
}
