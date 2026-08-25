import { useState } from "react";
import { AccountResponse, HealthResponse, OrderItem, PositionItem, RunSummary, TradeItem } from "../api/client";
import { LiquidityPanel } from "./LiquidityPanel";
import { PositionsPanel } from "./PositionsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";

/* The cockpit's lower support area. This used to be nine full-width
 * panels stacked one after another (positions, orders, trades, runs,
 * directional bias, missed opportunities, search, health) — the exact
 * "long telemetry page" shape this redesign is meant to eliminate. Same
 * panels, same data, but only one group renders at a time; the rest stay
 * a click away instead of permanently occupying scroll real estate. */

type TabId = "positions" | "orders" | "runs" | "bias" | "missed" | "search" | "system";

export function SupportTabs({
  account,
  accountError,
  positions,
  positionsError,
  positionsLoading,
  positionsUpdatedAt,
  orders,
  ordersError,
  ordersLoading,
  orderStatus,
  onOrderStatusChange,
  trades,
  tradesError,
  tradesLoading,
  runs,
  runsError,
  runsLoading,
  health,
  healthError,
  onSelectSymbol,
  onInspectOrder,
  onInspectTrade,
}: {
  account: AccountResponse | null;
  accountError?: string | null;
  positions: PositionItem[];
  positionsError: string | null;
  positionsLoading: boolean;
  positionsUpdatedAt?: Date | null;
  orders: OrderItem[];
  ordersError: string | null;
  ordersLoading: boolean;
  orderStatus: "open" | "closed" | "all";
  onOrderStatusChange: (status: "open" | "closed" | "all") => void;
  trades: TradeItem[];
  tradesError: string | null;
  tradesLoading: boolean;
  runs: RunSummary[];
  runsError: string | null;
  runsLoading: boolean;
  health: HealthResponse | null;
  healthError: string | null;
  onSelectSymbol?: (symbol: string) => void;
  onInspectOrder?: (order: OrderItem) => void;
  onInspectTrade?: (trade: TradeItem) => void;
}) {
  const [tab, setTab] = useState<TabId>("positions");

  const tabs: { id: TabId; label: string; badge?: number }[] = [
    { id: "positions", label: "Positions & Risk", badge: positions.length || undefined },
    { id: "orders", label: "Orders & Trades", badge: orders.length || undefined },
    { id: "runs", label: "Runs", badge: runs.length || undefined },
    { id: "bias", label: "Directional Bias" },
    { id: "missed", label: "Missed Opportunities" },
    { id: "search", label: "Search" },
    { id: "system", label: "System" },
  ];

  return (
    <div>
      <div className="flex w-full max-w-full items-center gap-1 overflow-x-auto mb-3 border-b border-border">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`flex-shrink-0 px-3 py-2 text-[0.72rem] font-semibold uppercase tracking-wide border-b-2 -mb-px whitespace-nowrap ${
              tab === t.id ? "border-accent text-accent" : "border-transparent text-dim hover:text-ink"
            }`}
          >
            {t.label}
            {t.badge ? <span className="ml-1.5 text-dim font-normal normal-case">({t.badge})</span> : null}
          </button>
        ))}
      </div>

      {tab === "positions" && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          <LiquidityPanel account={account} accountError={accountError} positions={positions} />
          <PositionsPanel positions={positions} error={positionsError} loading={positionsLoading} updatedAt={positionsUpdatedAt} onSelectSymbol={onSelectSymbol} />
        </div>
      )}

      {tab === "orders" && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          <OrdersPanel
            orders={orders}
            error={ordersError}
            loading={ordersLoading}
            status={orderStatus}
            onStatusChange={onOrderStatusChange}
            onInspect={onInspectOrder}
          />
          <TradesPanel trades={trades} error={tradesError} loading={tradesLoading} onInspect={onInspectTrade} />
        </div>
      )}

      {tab === "runs" && <RunsPanel runs={runs} error={runsError} loading={runsLoading} />}
      {tab === "bias" && <DirectionalBiasPanel />}
      {tab === "missed" && <MissedOpportunitiesPanel onSelectSymbol={onSelectSymbol} />}
      {tab === "search" && <SearchPanel />}
      {tab === "system" && <HealthPanel health={health} error={healthError} />}
    </div>
  );
}
