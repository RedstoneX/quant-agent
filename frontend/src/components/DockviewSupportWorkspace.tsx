import { useCallback, useRef } from "react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { LiquidityPanel } from "./LiquidityPanel";
import { PositionsPanel } from "./PositionsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";

/* Desktop-only (`xl:`+) resizable/draggable/poppable support workspace —
 * the operator's explicit, scoped approval for `dockview-react` (see
 * docs/WORK.md's Stage 6g entry / the design plan's Section 5): "Use
 * Dockview on desktop for the supporting workspace... The primary trading
 * state remains deliberately composed; customization belongs to the
 * supporting analytical workspace." This is the ONLY place in the product
 * that mounts Dockview — the primary cockpit (App.tsx's candidate-rail /
 * chart / Decision Room grid) is never wrapped in it, and this component
 * itself is never rendered below the `xl` breakpoint (App.tsx renders
 * `SupportTabs` there instead — see the `useIsDesktop` gate at the call
 * site).
 *
 * Every panel reads live state from `SupportWorkspaceContext` rather than
 * Dockview `params` (a static panel-config mechanism, not a reactive data
 * feed) — the exact same props `SupportTabs` (the iPad fallback) threads
 * through directly, so both surfaces stay in sync with zero duplicated
 * data-fetching. */

function PositionsRiskPanel() {
  const s = useSupportWorkspace();
  return (
    <div className="p-3 h-full overflow-y-auto grid grid-cols-1 2xl:grid-cols-2 gap-3">
      <LiquidityPanel account={s.account} accountError={s.accountError} positions={s.positions} />
      <PositionsPanel positions={s.positions} error={s.positionsError} loading={s.positionsLoading} updatedAt={s.positionsUpdatedAt} />
    </div>
  );
}

function OrdersTradesPanel() {
  const s = useSupportWorkspace();
  return (
    <div className="p-3 h-full overflow-y-auto grid grid-cols-1 2xl:grid-cols-2 gap-3">
      <OrdersPanel orders={s.orders} error={s.ordersError} loading={s.ordersLoading} status={s.orderStatus} onStatusChange={s.onOrderStatusChange} />
      <TradesPanel trades={s.trades} error={s.tradesError} loading={s.tradesLoading} />
    </div>
  );
}

function RunsWorkspacePanel() {
  const s = useSupportWorkspace();
  return (
    <div className="p-3 h-full overflow-y-auto">
      <RunsPanel runs={s.runs} error={s.runsError} loading={s.runsLoading} />
    </div>
  );
}

function BiasWorkspacePanel() {
  return (
    <div className="p-3 h-full overflow-y-auto">
      <DirectionalBiasPanel />
    </div>
  );
}

function MissedWorkspacePanel() {
  return (
    <div className="p-3 h-full overflow-y-auto">
      <MissedOpportunitiesPanel />
    </div>
  );
}

function SearchWorkspacePanel() {
  return (
    <div className="p-3 h-full overflow-y-auto">
      <SearchPanel />
    </div>
  );
}

function SystemWorkspacePanel() {
  const s = useSupportWorkspace();
  return (
    <div className="p-3 h-full overflow-y-auto">
      <HealthPanel health={s.health} error={s.healthError} />
    </div>
  );
}

const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  positions: PositionsRiskPanel,
  orders: OrdersTradesPanel,
  runs: RunsWorkspacePanel,
  bias: BiasWorkspacePanel,
  missed: MissedWorkspacePanel,
  search: SearchWorkspacePanel,
  system: SystemWorkspacePanel,
};

const DEFAULT_PANELS: { id: string; title: string }[] = [
  { id: "positions", title: "Positions & Risk" },
  { id: "orders", title: "Orders & Trades" },
  { id: "runs", title: "Runs" },
  { id: "bias", title: "Directional Bias" },
  { id: "missed", title: "Missed Opportunities" },
  { id: "search", title: "Search" },
  { id: "system", title: "System" },
];

// Pure client-side UI preference — never a second source of trading truth
// (docs/OUTCOME.md: "UI/journal/search state must not become a second
// authoritative trading-memory system"). Losing or clearing this key has
// zero effect on anything the product needs to function; it only restores
// which support panel the operator last had open where.
const LAYOUT_STORAGE_KEY = "qamc.dockview.support-workspace.v1";

function loadSavedLayout(): unknown | null {
  try {
    const raw = localStorage.getItem(LAYOUT_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function resetSupportWorkspaceLayout() {
  try {
    localStorage.removeItem(LAYOUT_STORAGE_KEY);
  } catch {
    // best-effort — a storage failure here must never break the workspace
  }
}

function buildDefaultLayout(api: DockviewApi) {
  DEFAULT_PANELS.forEach(({ id, title }) => {
    api.addPanel({ id, component: id, title });
  });
  api.panels[0]?.api.setActive();
}

export function DockviewSupportWorkspace() {
  const apiRef = useRef<DockviewApi | null>(null);

  const handleReset = useCallback(() => {
    resetSupportWorkspaceLayout();
    const api = apiRef.current;
    if (!api) return;
    [...api.panels].forEach((p) => api.removePanel(p));
    buildDefaultLayout(api);
  }, []);

  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    const saved = loadSavedLayout();
    if (saved) {
      try {
        event.api.fromJSON(saved as Parameters<typeof event.api.fromJSON>[0]);
      } catch {
        // A corrupted/incompatible saved layout must never block the
        // workspace from rendering — fall through to the default build.
      }
    }
    if (event.api.panels.length === 0) {
      // Default layout = today's tab-strip order, as one tabbed group —
      // the operator's explicit "not a chaotic customizable desktop by
      // default" requirement. The operator opts into a real multi-pane
      // workstation by dragging a tab out; nothing is pre-arranged into
      // separate panes.
      buildDefaultLayout(event.api);
    }
    event.api.onDidLayoutChange(() => {
      try {
        localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(event.api.toJSON()));
      } catch {
        // best-effort persistence only
      }
    });
  }, []);

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[0.68rem] text-dim uppercase tracking-wide font-semibold">
          Support workspace — drag tabs to resize/rearrange
        </span>
        <button type="button" onClick={handleReset} className="text-[0.7rem] text-accent underline">
          Reset layout
        </button>
      </div>
      <div className="h-[520px] rounded-lg border border-border overflow-hidden">
        <DockviewReact className="dockview-theme-qamc" onReady={onReady} components={PANEL_COMPONENTS} />
      </div>
    </div>
  );
}
