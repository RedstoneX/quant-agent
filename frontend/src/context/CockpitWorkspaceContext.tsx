import { createContext, useContext } from "react";
import { HoldingWhyResponse, MacroBroaderContext, RunFunnelResponse, RunSummary, TradeItem } from "../api/client";

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
  /** "Why do we hold this" for `chartSymbol`, fetched in App.tsx so it
   * keeps arriving while the Why tab is a BACKGROUND tab — the content is
   * already there when the owner switches to it (his requirement,
   * 2026-09-18). `holdingWhyError` is already a plain sentence, including
   * the "we never bought this name" case; never render it as a code. */
  holdingWhy: HoldingWhyResponse | null;
  holdingWhyError: string | null;
  holdingWhyLoading: boolean;
  /** Today's sessions + the last-known regime — Account and Sessions
   * Dockview panes read these. Mobile still renders the same strips from
   * App.tsx; desktop no longer keeps them as page chrome. */
  todaysRuns: RunSummary[];
  todaysFunnels: Record<string, RunFunnelResponse | null>;
  todaysTrades: TradeItem[];
  selectedRunId: string | null;
  autoFollow: boolean;
  onSelectSession: (runId: string) => void;
  onFollowLatest: () => void;
  onSelectTrade: (trade: TradeItem) => void;
  regime: { macro: MacroBroaderContext; asOf: string | null } | null;
}

const CockpitWorkspaceContext = createContext<CockpitWorkspaceState | null>(null);
export const CockpitWorkspaceProvider = CockpitWorkspaceContext.Provider;

export function useCockpitWorkspace(): CockpitWorkspaceState {
  const value = useContext(CockpitWorkspaceContext);
  if (!value) throw new Error("useCockpitWorkspace must be used within CockpitWorkspaceProvider");
  return value;
}
