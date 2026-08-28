import { createContext, useContext } from "react";
import { AccountResponse, HealthResponse, OrderItem, PositionItem, RunSummary, TradeItem } from "../api/client";

/* Live app state for the Dockview support workspace's panels. Dockview's
 * own `params` mechanism is meant for static panel-instantiation config,
 * not a per-poll-cycle reactive data feed — a plain React Context is the
 * idiomatic way to keep every panel component subscribed to the same
 * live state SupportTabs (the iPad fallback) already threads through
 * props, without re-deriving or duplicating any of it. */
export interface SupportWorkspaceState {
  account: AccountResponse | null;
  accountError: string | null;
  positions: PositionItem[];
  positionsError: string | null;
  positionsLoading: boolean;
  positionsUpdatedAt: Date | null;
  orders: OrderItem[];
  ordersError: string | null;
  ordersLoading: boolean;
  orderStatus: "open" | "closed" | "all";
  onOrderStatusChange: (status: "open" | "closed" | "all") => void;
  /** Open broker orders — polled independently of `orderStatus` above (the
   * Orders panel's own display filter), so a stop-order lookup for the
   * charted/held symbol (PriceChartPanel, the Decision Room's holding
   * card) never goes stale just because the operator switched that filter
   * to "closed"/"all". See App.tsx's dedicated openOrders poll. */
  openOrders: OrderItem[];
  trades: TradeItem[];
  tradesError: string | null;
  tradesLoading: boolean;
  runs: RunSummary[];
  runsError: string | null;
  runsLoading: boolean;
  health: HealthResponse | null;
  healthError: string | null;
  onSelectSymbol?: (symbol: string) => void;
  /** Position-panel-specific symbol click: charts the symbol and updates
   * the Decision Room/detail pane in place. Deliberately distinct from
   * `onSelectSymbol` above (which also opens the candidate-detail modal
   * for panels like Missed Opportunities, where drilling into a specific
   * run's evidence is exactly what a click should do) — clicking a
   * POSITION must never open a modal (cockpit trader rework, item 2). */
  onSelectPositionSymbol?: (symbol: string) => void;
  onInspectOrder?: (order: OrderItem) => void;
  onInspectTrade?: (trade: TradeItem) => void;
}

const SupportWorkspaceContext = createContext<SupportWorkspaceState | null>(null);

export const SupportWorkspaceProvider = SupportWorkspaceContext.Provider;

export function useSupportWorkspace(): SupportWorkspaceState {
  const ctx = useContext(SupportWorkspaceContext);
  if (!ctx) throw new Error("useSupportWorkspace must be used within SupportWorkspaceProvider");
  return ctx;
}
