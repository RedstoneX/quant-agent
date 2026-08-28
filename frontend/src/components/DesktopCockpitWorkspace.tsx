import { useCallback, useRef } from "react";
import { Button, Card, Text } from "@tremor/react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useCockpitWorkspace } from "../context/CockpitWorkspaceContext";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { useModalActions } from "../context/ModalContext";
import { CandidateRail } from "./CandidateRail";
import { PriceChartPanel } from "./PriceChartPanel";
import { DecisionRoomPanel } from "./DecisionRoomPanel";
import { LiquidityPanel } from "./LiquidityPanel";
import { PositionsPanel } from "./PositionsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";
import { Pill } from "./ui/Pill";

function CandidatesPane() {
  const state = useCockpitWorkspace();
  return <div className="h-full overflow-y-auto p-2"><CandidateRail funnel={state.funnel} loading={state.loading} error={state.error} updatedAt={state.updatedAt} selectedSymbol={state.chartSymbol} onSelectSymbol={state.onSelectSymbol} /></div>;
}

function ChartPane() {
  const state = useCockpitWorkspace();
  // Read-only broker positions, sourced from the same SupportWorkspace
  // state PositionsPane/LiquidityPane render — needed so the chart's
  // average-entry reference line (PriceChartPanel::entryPriceLine) actually
  // has data to draw from in the desktop Dockview workspace, not just the
  // mobile/tablet pane.
  const support = useSupportWorkspace();
  const { openCandidateDetail } = useModalActions();
  const candidate = state.funnel?.candidates.find((item) => item.symbol === state.chartSymbol);
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden p-2">
      <Card className="mb-2 flex !bg-panel-alt !p-2.5 !ring-border">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <span className="font-bold">{state.chartSymbol || "Market"}</span>
          {candidate && <><Pill text={candidate.direction} /><Pill text={candidate.order_status || (candidate.executed ? "executed" : "not executed")} /></>}
          {!candidate && <Text>Market context; no selected-run candidate evidence.</Text>}
          {candidate && state.funnel && (
            <Button className="ml-auto" size="xs" variant="light" color="cyan" onClick={() => openCandidateDetail(state.funnel!.run_id, candidate.symbol)}>Lifecycle &rarr;</Button>
          )}
        </div>
      </Card>
      <div className="min-h-0 flex-1"><PriceChartPanel symbol={state.chartSymbol} trades={state.chartTrades} positions={support.positions} /></div>
    </div>
  );
}

function DecisionPane() {
  const state = useCockpitWorkspace();
  return <div className="h-full overflow-y-auto p-2"><DecisionRoomPanel funnel={state.funnel} symbol={state.chartSymbol} loading={state.loading} error={state.error} updatedAt={state.updatedAt} /></div>;
}

// Positions and Liquidity were previously one combined pane (a vertical
// stack of both panels inside a single Dockview tab). The owner asked for
// them as two independently dockable panels — each can now be dragged,
// resized, or tabbed on its own instead of always moving together.
function PositionsPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><PositionsPanel positions={state.positions} error={state.positionsError} loading={state.positionsLoading} updatedAt={state.positionsUpdatedAt} onSelectSymbol={state.onSelectSymbol} /></div>;
}

function LiquidityPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><LiquidityPanel account={state.account} accountError={state.accountError} positions={state.positions} /></div>;
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
function SearchPane() { return <div className="h-full overflow-y-auto p-2"><SearchPanel /></div>; }
function SystemPane() { const state = useSupportWorkspace(); return <div className="h-full overflow-y-auto p-2"><HealthPanel health={state.health} error={state.healthError} /></div>; }

const COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  candidates: CandidatesPane,
  chart: ChartPane,
  decision: DecisionPane,
  positions: PositionsPane,
  liquidity: LiquidityPane,
  orders: OrdersPane,
  trades: TradesPane,
  runs: RunsPane,
  bias: BiasPane,
  missed: MissedPane,
  search: SearchPane,
  system: SystemPane,
};

// Bumped v1 -> v2: splitting the combined "Positions & Liquidity" panel
// into two separate panels (below) changes the saved layout's panel-id
// shape, so a v1 layout persisted from localStorage would reference a
// "positions" panel that no longer means the same thing and would never
// resolve the new "liquidity" panel at all. A fresh key means every
// browser simply falls back to buildDefaultLayout below instead of
// throwing/half-rendering against a stale shape.
const STORAGE_KEY = "qamc.dockview.cockpit.v2";

function buildDefaultLayout(api: DockviewApi) {
  api.addPanel({ id: "chart", component: "chart", title: "Chart" });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "chart", direction: "left" }, initialWidth: 320 });
  api.addPanel({ id: "positions", component: "positions", title: "Positions", position: { referencePanel: "chart", direction: "within" }, inactive: true });
  api.addPanel({ id: "liquidity", component: "liquidity", title: "Liquidity", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  api.addPanel({ id: "missed", component: "missed", title: "Missed", position: { referencePanel: "chart", direction: "within" }, inactive: true });
  api.addPanel({ id: "decision", component: "decision", title: "Decision Room", position: { referencePanel: "chart", direction: "right" }, initialWidth: 400 });
  for (const [id, title] of [["orders", "Orders"], ["trades", "Trades"], ["runs", "Runs"], ["bias", "Directional Bias"], ["search", "Search"], ["system", "System"]]) {
    api.addPanel({ id, component: id, title, position: { referencePanel: "chart", direction: "within" }, inactive: true });
  }
  api.getPanel("chart")?.api.setActive();
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
