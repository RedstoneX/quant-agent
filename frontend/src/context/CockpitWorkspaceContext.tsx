import { createContext, useContext } from "react";
import { RunFunnelResponse, TradeItem } from "../api/client";

export interface CockpitWorkspaceState {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  updatedAt: Date | null;
  chartSymbol: string | null;
  chartTrades: TradeItem[];
  onSelectSymbol: (symbol: string) => void;
  /** Fired on the desktop chart's own pan/zoom/timeframe/Reset-zoom
   * interactions — feeds App.tsx's "don't let auto-follow hijack the
   * chart while the operator is actively engaged" fix. See
   * PriceChartPanel's onUserInteraction prop. */
  onChartInteraction?: () => void;
  /** Quick "back to previous symbol" (owner request) — the single symbol
   * charted immediately before the current one, or null when there's
   * nothing to go back to. See App.tsx's chartSymbol wrapper. */
  previousChartSymbol?: string | null;
  onGoBackSymbol?: () => void;
}

const CockpitWorkspaceContext = createContext<CockpitWorkspaceState | null>(null);
export const CockpitWorkspaceProvider = CockpitWorkspaceContext.Provider;

export function useCockpitWorkspace(): CockpitWorkspaceState {
  const value = useContext(CockpitWorkspaceContext);
  if (!value) throw new Error("useCockpitWorkspace must be used within CockpitWorkspaceProvider");
  return value;
}
