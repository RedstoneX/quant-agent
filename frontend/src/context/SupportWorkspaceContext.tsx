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
  /** Charts the symbol in place — nothing else. Every symbol-click
   * affordance in this workspace (Positions, Orders/Trades symbol cell,
   * Missed Opportunities, Search's symbol column) is wired to this same
   * modal-free callback; see App.tsx's chartPositionSymbol for the
   * owner's no-popup-on-a-symbol-click rule this exists to satisfy. A
   * field named `onSelectSymbol` used to sit here too, wired to a
   * variant that conditionally opened the candidate-detail modal — that
   * was the cause of Missed Opportunities' unpredictable click, and it
   * was removed rather than fixed in place once nothing needed it. */
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
