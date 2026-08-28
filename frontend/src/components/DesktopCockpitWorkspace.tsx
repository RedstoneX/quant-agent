import { useCallback, useRef } from "react";
import { Button, Card, Text } from "@tremor/react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useCockpitWorkspace } from "../context/CockpitWorkspaceContext";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { useModalActions } from "../context/ModalContext";
import { CandidateRail } from "./CandidateRail";
import { PriceChartPanel } from "./PriceChartPanel";
import { PositionHoldingStrip } from "./PositionHoldingStrip";
import { DecisionSummaryLine } from "./DecisionSummaryLine";
import { PositionsPanel } from "./PositionsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";
import { Pill } from "./ui/Pill";
import { Panel, StateMessage } from "./ui/Panel";

// Item 2 of cockpit pass 3: the middle column of the bottom row is
// deliberately empty by default — "free for him to populate" in the
// owner's own words, not a panel we picked for him. Built from the same
// approved Panel/StateMessage primitives every other pane in this
// workspace uses (no bespoke graphic), reusing the existing ●/◐/■/○
// glyph vocabulary (see ui/Panel.tsx's StateMessage) rather than
// inventing a new icon for "nothing here yet."
function WorkspaceSlotPane() {
  return (
    <div className="h-full overflow-y-auto p-2">
      <Panel title="Workspace">
        <StateMessage
          hero
          glyph="○"
          text="Empty by default — drag any panel's tab here to fill this column, or use “Reset layout” to restore the default."
        />
      </Panel>
    </div>
  );
}

// Item 1 of the cockpit trader rework: Positions is the panel a trader
// lands on, not one tab among several — see PositionsPane below and
// buildDefaultLayout's placement of it as the leftmost, active-by-default
// group.
function PositionsPane() {
  const state = useSupportWorkspace();
  return (
    <div className="h-full overflow-y-auto p-2">
      <PositionsPanel
        positions={state.positions}
        error={state.positionsError}
        loading={state.positionsLoading}
        updatedAt={state.positionsUpdatedAt}
        onSelectSymbol={state.onSelectPositionSymbol}
      />
    </div>
  );
}

function CandidatesPane() {
  const state = useCockpitWorkspace();
  return <div className="h-full overflow-y-auto p-2"><CandidateRail funnel={state.funnel} loading={state.loading} error={state.error} updatedAt={state.updatedAt} selectedSymbol={state.chartSymbol} onSelectSymbol={state.onSelectSymbol} /></div>;
}

// Owner correction: the Decision Room panel is gone (see PR description).
// What used to be its "position I hold" answer is now
// PositionHoldingStrip — an inline compact strip under the chart, never a
// popup/modal/drawer — and its "what did this run's candidate do" answer
// is DecisionSummaryLine, a single line that renders nothing at all
// unless there is real content. Both sit directly under the chart, in the
// same pane, so nothing ever covers the candles.
function ChartPane() {
  const state = useCockpitWorkspace();
  // Read-only broker positions/orders/trades, sourced from the same
  // SupportWorkspace state PositionsPane/OrdersPane render — needed so the
  // chart's average-entry (entryPriceLine) and protective-stop
  // (positionStopLine) reference lines actually have data to draw from in
  // the desktop Dockview workspace, not just the mobile/iPad pane.
  const support = useSupportWorkspace();
  const { openCandidateDetail } = useModalActions();
  const candidate = state.funnel?.candidates.find((item) => item.symbol === state.chartSymbol);
  const heldPosition = state.chartSymbol ? support.positions.find((p) => p.symbol === state.chartSymbol) : undefined;
  return (
    <div className="flex h-full min-w-0 flex-col overflow-x-hidden overflow-y-auto p-2 gap-2">
      <Card className="flex !bg-panel-alt !p-2.5 !ring-border">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <span className="font-bold">{state.chartSymbol || "Market"}</span>
          {candidate && <><Pill text={candidate.direction} /><Pill text={candidate.order_status || (candidate.executed ? "executed" : "not executed")} /></>}
          {!candidate && <Text>Market context; no selected-run candidate evidence.</Text>}
          {candidate && state.funnel && (
            <Button className="ml-auto" size="xs" variant="light" color="cyan" onClick={() => openCandidateDetail(state.funnel!.run_id, candidate.symbol)}>Lifecycle &rarr;</Button>
          )}
        </div>
      </Card>
      {heldPosition && <PositionHoldingStrip position={heldPosition} openOrders={support.openOrders} trades={support.trades} />}
      <DecisionSummaryLine funnel={state.funnel} symbol={state.chartSymbol} />
      <div className="min-h-[320px] flex-1">
        <PriceChartPanel
          symbol={state.chartSymbol}
          trades={state.chartTrades}
          positions={support.positions}
          openOrders={support.openOrders}
          positionTrades={support.trades}
        />
      </div>
    </div>
  );
}

function OrdersPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><OrdersPanel orders={state.orders} error={state.ordersError} loading={state.ordersLoading} status={state.orderStatus} onStatusChange={state.onOrderStatusChange} onInspect={state.onInspectOrder} /></div>;
}

function TradesPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><TradesPanel trades={state.trades} error={state.tradesError} loading={state.tradesLoading} onInspect={state.onInspectTrade} /></div>;
}

function RunsPane() { const state = useSupportWorkspace(); return <div className="h-full overflow-y-auto p-2"><RunsPanel runs={state.runs} error={state.runsError} loading={state.runsLoading} /></div>; }
function BiasPane() { return <div className="h-full overflow-y-auto p-2"><DirectionalBiasPanel /></div>; }
function MissedPane() { const state = useSupportWorkspace(); return <div className="h-full overflow-y-auto p-2"><MissedOpportunitiesPanel onSelectSymbol={state.onSelectSymbol} /></div>; }

// Item 13 (cockpit trader rework): System and Search — named by the owner
// as "not trading" — used to each be their own top-level tab in this
// workspace's chart-group tab strip. Folded into one Diagnostics tab
// instead of standing on their own; same two panels, just one click away
// together rather than two clicks apart.
function DiagnosticsPane() {
  const state = useSupportWorkspace();
  return (
    <div className="h-full overflow-y-auto p-2 flex flex-col gap-3">
      <HealthPanel health={state.health} error={state.healthError} />
      <SearchPanel />
    </div>
  );
}

const COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  positions: PositionsPane,
  candidates: CandidatesPane,
  chart: ChartPane,
  orders: OrdersPane,
  trades: TradesPane,
  runs: RunsPane,
  bias: BiasPane,
  missed: MissedPane,
  diagnostics: DiagnosticsPane,
  workspaceSlot: WorkspaceSlotPane,
};

// Bumped v4 -> v5 (cockpit pass 3, item 2): the default workspace shape
// changed from one row of three columns (Positions | Chart | Orders) to
// two rows — a full-width Chart row on top, then a Positions/free/Orders
// three-column row below (see buildDefaultLayout). A v4 layout persisted
// from localStorage is still a perfectly valid dockview shape (nothing
// referenced by it was removed), so this bump exists purely to change
// what EVERY BROWSER sees as the default on first load / after "Reset
// layout" — without it, anyone who already has a saved v4 layout would
// keep seeing the old one-row shape and never notice the new default
// exists. Existing saved layouts are otherwise untouched: dockview state
// is genuinely additive/positional, not a fixed schema, so a v4 blob
// would have loaded fine under this key too. (v3 -> v4 / v2 -> v3
// history: the Decision Room panel was removed entirely — its content
// moved inline under the chart as PositionHoldingStrip/DecisionSummaryLine
// or already exists on the Research Desk as DecisionDeltaPanel; Positions
// moved from a background tab inside the chart group to its own leftmost,
// active-by-default group; Liquidity left the workspace entirely for a
// compact header row — item 9; System+Search merged into one Diagnostics
// panel — item 13.)
const STORAGE_KEY = "qamc.dockview.cockpit.v5";

// Item 2 of cockpit pass 3 ("a better DEFAULT, not a lock" — every panel
// below stays exactly as movable/dockable/resizable as it always was;
// this only changes what a brand-new session (or "Reset layout") starts
// from). The owner's own framing: chart alone across the top because
// it's the first thing he looks at, then a three-column row underneath —
// Positions left, Orders right, middle column genuinely empty for him to
// fill rather than a panel we picked on his behalf.
function buildDefaultLayout(api: DockviewApi) {
  // Top row: the chart, full width — nothing else in this row.
  api.addPanel({ id: "chart", component: "chart", title: "Chart" });
  // The non-trading analysis panels ride along as background tabs on the
  // chart group (unchanged from before this pass) rather than moving to
  // the bottom row — they pair conceptually with "studying a symbol/run",
  // not with the Positions/Orders tables.
  for (const [id, title] of [["missed", "Missed"], ["runs", "Runs"], ["bias", "Directional Bias"], ["diagnostics", "Diagnostics"]]) {
    api.addPanel({ id, component: id, title, position: { referencePanel: "chart", direction: "within" }, inactive: true });
  }

  // Bottom row: Positions (left) | free workspace slot (middle) | Orders
  // (right) — a real second row (direction: "below"), not more tabs
  // folded into the chart group, so it gets its own genuine height and
  // its own independent resize handle against the chart row above it.
  api.addPanel({
    id: "positions",
    component: "positions",
    title: "Positions",
    position: { referencePanel: "chart", direction: "below" },
    initialWidth: 360,
    initialHeight: 480,
  });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  api.addPanel({ id: "workspaceSlot", component: "workspaceSlot", title: "Workspace", position: { referencePanel: "positions", direction: "right" } });
  api.addPanel({ id: "orders", component: "orders", title: "Orders", position: { referencePanel: "workspaceSlot", direction: "right" }, initialWidth: 360 });
  api.addPanel({ id: "trades", component: "trades", title: "Trades", position: { referencePanel: "orders", direction: "within" }, inactive: true });

  api.getPanel("positions")?.api.setActive();
}

export function DesktopCockpitWorkspace() {
  const apiRef = useRef<DockviewApi | null>(null);
  const reset = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    const api = apiRef.current;
    if (!api) return;
    [...api.panels].forEach((panel) => api.removePanel(panel));
    buildDefaultLayout(api);
  }, []);
  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) event.api.fromJSON(JSON.parse(saved));
    } catch { localStorage.removeItem(STORAGE_KEY); }
    if (!event.api.panels.length) buildDefaultLayout(event.api);
    event.api.onDidLayoutChange(() => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(event.api.toJSON())); } catch { /* UI-only best effort */ }
    });
  }, []);

  return (
    <div className="px-3 pb-3">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-dim">Trading workspace — move, resize or dock panels</span>
        <button type="button" onClick={reset} className="text-xs text-accent underline">Reset layout</button>
      </div>
      {/* Item 2 of cockpit pass 3: two genuinely tall rows instead of one
          viewport-locked row split three ways. Before this, Positions/
          Chart/Orders all shared ONE row capped at `100vh - chrome`, so
          getting more room for the chart meant shrinking something else
          inside that same fixed budget — there was nowhere else for the
          height to come from. Now the chart row alone keeps that full
          budget (`max(560px, 100vh-chrome)` — never shorter than before),
          and the Positions/workspace/Orders row underneath adds a further
          480px on top of it, so the container is deliberately taller than
          one screen. The owner explicitly wants to reach for that with
          scroll rather than have every panel shrunk to fit one viewport —
          nothing here clips the page itself; `overflow-hidden` below only
          rounds dockview's own corners, it does not cap page height. */}
      <div className="h-[calc(max(560px,calc(100vh-var(--chrome-h)))+480px)] overflow-hidden rounded-lg border border-border">
        <DockviewReact className="dockview-theme-qamc" onReady={onReady} components={COMPONENTS} />
      </div>
    </div>
  );
}
