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
};

// Bumped v3 -> v4: the Decision Room panel is removed from the workspace
// entirely (owner correction — see PR description; its content either
// moved inline under the chart as PositionHoldingStrip/DecisionSummaryLine
// or already exists on the Research Desk as DecisionDeltaPanel). A v3
// layout persisted from localStorage would still reference a "decision"
// panel id that no longer resolves to any component. A fresh key means
// every browser falls back to buildDefaultLayout below instead of
// throwing/half-rendering against a stale shape. (v2 -> v3 history:
// Positions moved from a background tab inside the chart group to its own
// leftmost, active-by-default group; Liquidity left the workspace
// entirely for a compact header row — item 9; System+Search merged into
// one Diagnostics panel — item 13.)
const STORAGE_KEY = "qamc.dockview.cockpit.v4";

function buildDefaultLayout(api: DockviewApi) {
  // Positions leads (item 1): leftmost group, and the one made active
  // below — the panel a trader actually lands on, not a background tab.
  api.addPanel({ id: "positions", component: "positions", title: "Positions", initialWidth: 360 });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  api.addPanel({ id: "chart", component: "chart", title: "Chart", position: { referencePanel: "positions", direction: "right" } });
  // Orders/Trades: still one click away on the right, not buried behind
  // Diagnostics with the non-trading panels.
  api.addPanel({ id: "orders", component: "orders", title: "Orders", position: { referencePanel: "chart", direction: "right" }, initialWidth: 360 });
  api.addPanel({ id: "trades", component: "trades", title: "Trades", position: { referencePanel: "orders", direction: "within" }, inactive: true });
  for (const [id, title] of [["missed", "Missed"], ["runs", "Runs"], ["bias", "Directional Bias"], ["diagnostics", "Diagnostics"]]) {
    api.addPanel({ id, component: id, title, position: { referencePanel: "chart", direction: "within" }, inactive: true });
  }
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
      <div className="h-[max(560px,calc(100vh-var(--chrome-h)))] overflow-hidden rounded-lg border border-border">
        <DockviewReact className="dockview-theme-qamc" onReady={onReady} components={COMPONENTS} />
      </div>
    </div>
  );
}
