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
}

const CockpitWorkspaceContext = createContext<CockpitWorkspaceState | null>(null);
export const CockpitWorkspaceProvider = CockpitWorkspaceContext.Provider;

export function useCockpitWorkspace(): CockpitWorkspaceState {
  const value = useContext(CockpitWorkspaceContext);
  if (!value) throw new Error("useCockpitWorkspace must be used within CockpitWorkspaceProvider");
  return value;
}
