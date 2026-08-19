import { useEffect, useState } from "react";
import {
  api,
  AccountResponse,
  HealthResponse,
  OrderItem,
  PositionItem,
  RunFunnelResponse,
  RunSummary,
  TradeItem,
} from "./api/client";
import { usePoll } from "./lib/usePoll";
import { ModalProvider, useModalState } from "./context/ModalContext";
import { TopStrip } from "./components/TopStrip";
import { DecisionFunnelPanel } from "./components/DecisionFunnelPanel";
import { WatchlistPanel } from "./components/WatchlistPanel";
import { PriceChartPanel } from "./components/PriceChartPanel";
import { LiquidityPanel } from "./components/LiquidityPanel";
import { PositionsPanel } from "./components/PositionsPanel";
import { OrdersPanel, useOrderStatus } from "./components/OrdersPanel";
import { TradesPanel } from "./components/TradesPanel";
import { RunsPanel } from "./components/RunsPanel";
import { MissedOpportunitiesPanel } from "./components/MissedOpportunitiesPanel";
import { JournalPanel } from "./components/JournalPanel";
import { SearchPanel } from "./components/SearchPanel";
import { HealthPanel } from "./components/HealthPanel";
import { RunDetailModal } from "./components/RunDetailModal";
import { CandidateDetailModal } from "./components/CandidateDetailModal";

export default function App() {
  const [account, setAccount] = useState<AccountResponse | null>(null);
  const [positions, setPositions] = useState<PositionItem[]>([]);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [orderStatus, setOrderStatus] = useOrderStatus();
  const [orders, setOrders] = useState<OrderItem[]>([]);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [trades, setTrades] = useState<TradeItem[]>([]);
  const [tradesError, setTradesError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [funnel, setFunnel] = useState<RunFunnelResponse | null>(null);
  const [funnelError, setFunnelError] = useState<string | null>(null);
  const [funnelLoading, setFunnelLoading] = useState(true);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const { state: modalState, value: modalActions } = useModalState();

  usePoll(() => {
    api
      .account()
      .then(setAccount)
      .catch((err) => setAccount({ cash: null, portfolio_value: null, last_equity: null, daily_pnl: null, daily_pnl_pct: null, paper: null, history: [], liquidity: null, error: err.message }));

    api
      .positions()
      .then((r) => {
        setPositions(r.positions);
        setPositionsError(r.error);
      })
      .catch((err) => setPositionsError(err.message));

    api
      .trades(30)
      .then((r) => {
        setTrades(r.trades);
        setTradesError(null);
      })
      .catch((err) => setTradesError(err.message));

    api
      .health()
      .then((h) => {
        setHealth(h);
        setHealthError(null);
        setUpdatedAt(new Date());
      })
      .catch((err) => setHealthError(err.message));

    api
      .runs(1)
      .then((r) => {
        if (!r.runs.length) {
          setFunnel(null);
          setFunnelLoading(false);
          return;
        }
        return api.runFunnel(r.runs[0].run_id).then((f) => {
          setFunnel(f);
          setFunnelError(null);
          setFunnelLoading(false);
          if (!chartSymbol && f.candidates.length) setChartSymbol(f.candidates[0].symbol);
        });
      })
      .catch((err) => {
        setFunnelError(err.message);
        setFunnelLoading(false);
      });
  }, []);

  usePoll(() => {
    api
      .orders(orderStatus)
      .then((r) => {
        setOrders(r.orders);
        setOrdersError(r.error);
      })
      .catch((err) => setOrdersError(err.message));
  }, [orderStatus]);

  useEffect(() => {
    api
      .runs(25)
      .then((r) => setRuns(r.runs))
      .catch((err) => setRunsError(err.message));
  }, []);

  async function openJournalCandidate(dayRuns: RunSummary[], symbol: string) {
    if (!dayRuns.length) return;
    if (dayRuns.length === 1) {
      modalActions.openCandidateDetail(dayRuns[0].run_id, symbol);
      return;
    }
    const results = await Promise.all(
      dayRuns.map((r) =>
        api
          .runCandidates(r.run_id)
          .then((d) => ({ run: r, has: d.candidates.includes(symbol) }))
          .catch(() => ({ run: r, has: false }))
      )
    );
    const matches = results.filter((x) => x.has).map((x) => x.run);
    if (matches.length >= 1) modalActions.openCandidateDetail(matches[0].run_id, symbol);
  }

  return (
    <ModalProvider value={modalActions}>
      <TopStrip account={account} positions={positions} health={health} updatedAt={updatedAt} />

      <main className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3">
        <DecisionFunnelPanel funnel={funnel} loading={funnelLoading} error={funnelError} />
        <WatchlistPanel funnel={funnel} loading={funnelLoading} error={funnelError} onSelectSymbol={setChartSymbol} />
        <PriceChartPanel symbol={chartSymbol} />
        <LiquidityPanel account={account} positions={positions} />
        <PositionsPanel positions={positions} error={positionsError} loading={!account} />
        <MissedOpportunitiesPanel />
        <OrdersPanel
          orders={orders}
          error={ordersError}
          loading={!account}
          status={orderStatus}
          onStatusChange={setOrderStatus}
        />
        <TradesPanel trades={trades} error={tradesError} loading={!account} />
        <RunsPanel runs={runs} error={runsError} loading={runs.length === 0 && !runsError} />
        <JournalPanel onOpenCandidate={openJournalCandidate} />
        <SearchPanel />
        <HealthPanel health={health} error={healthError} />
      </main>

      <footer className="text-center text-[0.7rem] text-dim py-4 px-3">
        QAMC Mission Control is a read-only view. It cannot place, cancel, or modify orders, and its failure has no
        effect on trading.
      </footer>

      {modalState?.type === "run" && <RunDetailModal runId={modalState.runId} onClose={modalActions.closeModal} />}
      {modalState?.type === "candidate" && (
        <CandidateDetailModal
          runId={modalState.runId}
          symbol={modalState.symbol}
          onClose={modalActions.closeModal}
        />
      )}
    </ModalProvider>
  );
}
