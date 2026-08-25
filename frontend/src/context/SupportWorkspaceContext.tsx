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
}

const SupportWorkspaceContext = createContext<SupportWorkspaceState | null>(null);

export const SupportWorkspaceProvider = SupportWorkspaceContext.Provider;

export function useSupportWorkspace(): SupportWorkspaceState {
  const ctx = useContext(SupportWorkspaceContext);
  if (!ctx) throw new Error("useSupportWorkspace must be used within SupportWorkspaceProvider");
  return ctx;
}
