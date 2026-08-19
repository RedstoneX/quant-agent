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
import { HeroBand } from "./components/HeroBand";
import { DecisionStateBanner } from "./components/DecisionStateBanner";
import { CandidateRail } from "./components/CandidateRail";
import { DecisionRoomPanel } from "./components/DecisionRoomPanel";
import { PriceChartPanel } from "./components/PriceChartPanel";
import { SupportTabs } from "./components/SupportTabs";
import { DockviewSupportWorkspace } from "./components/DockviewSupportWorkspace";
import { SupportWorkspaceProvider } from "./context/SupportWorkspaceContext";
import { useIsDesktop } from "./lib/useIsDesktop";
import { useOrderStatus } from "./components/OrdersPanel";
import { JournalPanel } from "./components/JournalPanel";
import { RunDetailModal } from "./components/RunDetailModal";
import { CandidateDetailModal } from "./components/CandidateDetailModal";
import { Pill } from "./components/ui/Pill";

type View = "cockpit" | "journal";
type MobilePane = "watchlist" | "chart" | "decision";

/* Top-level view switcher — Cockpit (the live working surface) vs Journal
 * (the day-by-day narrative). Kept as two views rather than one more
 * panel in the cockpit stack: Journal is a full day's worth of reading on
 * its own and dilutes "what is happening right now" if it shares scroll
 * space with the cockpit. */
function ViewNav({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  return (
    <nav className="flex items-center gap-1 px-4 border-b border-border bg-bg">
      {(["cockpit", "journal"] as const).map((v) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          className={`px-3 py-2 text-[0.78rem] font-semibold uppercase tracking-wide border-b-2 -mb-px ${
            view === v ? "border-accent text-accent" : "border-transparent text-dim hover:text-ink"
          }`}
        >
          {v === "cockpit" ? "Cockpit" : "Journal"}
        </button>
      ))}
    </nav>
  );
}

const PANE_LABELS: Record<MobilePane, string> = {
  watchlist: "Candidates",
  chart: "Chart",
  decision: "Decision Room",
};

// Below the `xl` breakpoint (covers every iPad size, portrait or
// landscape) the three cockpit panes become an explicit tab strip instead
// of being squeezed side by side — a real tabbed surface, not a
// compressed desktop layout.
function PaneNav({ pane, onChange }: { pane: MobilePane; onChange: (p: MobilePane) => void }) {
  return (
    <div className="xl:hidden flex border-b border-border">
      {(["watchlist", "chart", "decision"] as const).map((p) => (
        <button
          key={p}
          type="button"
          onClick={() => onChange(p)}
          className={`flex-1 py-2 text-[0.72rem] font-semibold uppercase tracking-wide border-b-2 ${
            pane === p ? "border-accent text-accent" : "border-transparent text-dim"
          }`}
        >
          {PANE_LABELS[p]}
        </button>
      ))}
    </div>
  );
}

// Center-column header for whichever symbol is currently charted — derived
// only from the latest run's already-fetched funnel data (no extra
// fetch), so the chart never sits contextless above a bare candlestick.
function SelectedSymbolContext({
  funnel,
  symbol,
  onOpenDetail,
}: {
  funnel: RunFunnelResponse | null;
  symbol: string | null;
  onOpenDetail: () => void;
}) {
  if (!symbol) return null;
  const c = funnel?.candidates.find((x) => x.symbol === symbol);
  return (
    <div className="flex items-center gap-2 flex-wrap px-3.5 py-2 mb-3 rounded-lg border border-border bg-panel-alt">
      <span className="font-bold text-[0.95rem]">{symbol}</span>
      {c ? (
        <>
          <Pill text={c.direction} />
          {c.is_bearish_hedge && <Pill text="bearish_hedge" />}
          {c.executed ? (
            <Pill text="executed" />
          ) : c.reached_proposed_order ? (
            <Pill text={c.risk_modified ? "modified" : "proposed"} />
          ) : c.reached_pm_target ? (
            <Pill text="reached_pm" />
          ) : null}
          <button type="button" onClick={onOpenDetail} className="ml-auto text-accent underline text-[0.78rem]">
            Full drill-down &rarr;
          </button>
        </>
      ) : (
        <span className="text-dim text-[0.78rem]">not among the latest run&rsquo;s candidates</span>
      )}
    </div>
  );
}

export default function App() {
  // Truthful-freshness state: every poll target below tracks its data,
  // its current error (if the LATEST poll failed), and the timestamp of
  // its last successful fetch. A failed poll never overwrites previously
  // good data with nulls/empties — it only sets the error, so the UI can
  // render "STALE — last known data as of HH:MM" instead of silently
  // continuing to show old data as if it were current (or, worse,
  // blanking real data that's still the best information available).
  // This is the fix for the exact bug an operator screenshot exposed:
  // an EXECUTED Decision Room rendering as current underneath a visible
  // fetch-error banner. See docs/STATE.md's Stage 6g entry.
  const [account, setAccount] = useState<AccountResponse | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountUpdatedAt, setAccountUpdatedAt] = useState<Date | null>(null);
  const [positions, setPositions] = useState<PositionItem[]>([]);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [positionsUpdatedAt, setPositionsUpdatedAt] = useState<Date | null>(null);
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
  const [funnelUpdatedAt, setFunnelUpdatedAt] = useState<Date | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [view, setView] = useState<View>("cockpit");
  const [mobilePane, setMobilePane] = useState<MobilePane>("watchlist");

  const { state: modalState, value: modalActions } = useModalState();
  const isDesktop = useIsDesktop();

  usePoll(() => {
    api
      .account()
      .then((a) => {
        // A backend-reported failure (a.error set) is treated identically
        // to a network/fetch exception below: both are "this poll failed,"
        // and both must leave the last-good `account` object in place
        // rather than replacing it with an all-null husk.
        if (a.error) {
          setAccountError(a.error);
        } else {
          setAccount(a);
          setAccountError(null);
          setAccountUpdatedAt(new Date());
        }
      })
      .catch((err) => setAccountError(err.message));

    api
      .positions()
      .then((r) => {
        if (r.error) {
          setPositionsError(r.error);
        } else {
          setPositions(r.positions);
          setPositionsError(null);
          setPositionsUpdatedAt(new Date());
        }
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
          setFunnelError(null);
          setFunnelLoading(false);
          setFunnelUpdatedAt(new Date());
          return;
        }
        return api.runFunnel(r.runs[0].run_id).then((f) => {
          setFunnel(f);
          setFunnelError(null);
          setFunnelLoading(false);
          setFunnelUpdatedAt(new Date());
          if (!chartSymbol && f.candidates.length) setChartSymbol(f.candidates[0].symbol);
        });
      })
      .catch((err) => {
        // Deliberately does NOT clear `funnel` — see the freshness comment
        // above. CandidateRail/DecisionRoomPanel render the retained data
        // with an explicit "stale" Panel status instead of a blank error.
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
      <TopStrip account={account} accountError={accountError} health={health} updatedAt={updatedAt} />
      <ViewNav view={view} onChange={setView} />

      {view === "cockpit" && (
        <>
          <HeroBand account={account} accountError={accountError} positions={positions} funnel={funnel} />
          <DecisionStateBanner funnel={funnel} loading={funnelLoading} error={funnelError} updatedAt={funnelUpdatedAt} />

          <PaneNav pane={mobilePane} onChange={setMobilePane} />

          {/* The primary cockpit body — deliberately a fixed, non-dockable
              grid (not Dockview): this is the "answer at a glance" surface,
              customization belongs only to the support workspace below. */}
          <div className="grid grid-cols-1 xl:grid-cols-[300px_1fr_360px] gap-3 p-3 items-stretch">
            <div className={`${mobilePane === "watchlist" ? "block" : "hidden xl:block"} xl:max-h-[calc(100vh-150px)] xl:overflow-y-auto`}>
              <CandidateRail
                funnel={funnel}
                loading={funnelLoading}
                error={funnelError}
                updatedAt={funnelUpdatedAt}
                selectedSymbol={chartSymbol}
                onSelectSymbol={setChartSymbol}
              />
            </div>

            {/* min-w-0 is load-bearing: a CSS Grid `1fr` track defaults to
                minmax(auto, 1fr), so without it this column would size to
                its widest child's natural content width (the price chart)
                instead of shrinking to its assigned share of the row,
                pushing the Decision Room column past the viewport edge. */}
            <div className={`${mobilePane === "chart" ? "block" : "hidden xl:block"} min-w-0`}>
              <SelectedSymbolContext
                funnel={funnel}
                symbol={chartSymbol}
                onOpenDetail={() => chartSymbol && funnel && modalActions.openCandidateDetail(funnel.run_id, chartSymbol)}
              />
              <PriceChartPanel symbol={chartSymbol} trades={trades} />
            </div>

            <div className={`${mobilePane === "decision" ? "block" : "hidden xl:block"} xl:max-h-[calc(100vh-150px)] xl:overflow-y-auto`}>
              <DecisionRoomPanel funnel={funnel} loading={funnelLoading} error={funnelError} updatedAt={funnelUpdatedAt} />
            </div>
          </div>

          <div className="px-3 pb-3">
            {isDesktop ? (
              // Desktop-only, operator-approved: the support workspace
              // becomes a real resizable/draggable Dockview surface here.
              // Never mounted below the `xl` breakpoint — see useIsDesktop.
              <SupportWorkspaceProvider
                value={{
                  account,
                  accountError,
                  positions,
                  positionsError,
                  positionsLoading: !account && !positionsError,
                  positionsUpdatedAt,
                  orders,
                  ordersError,
                  ordersLoading: !account,
                  orderStatus,
                  onOrderStatusChange: setOrderStatus,
                  trades,
                  tradesError,
                  tradesLoading: !account,
                  runs,
                  runsError,
                  runsLoading: runs.length === 0 && !runsError,
                  health,
                  healthError,
                }}
              >
                <DockviewSupportWorkspace />
              </SupportWorkspaceProvider>
            ) : (
              <SupportTabs
                account={account}
                accountError={accountError}
                positions={positions}
                positionsError={positionsError}
                positionsLoading={!account && !positionsError}
                positionsUpdatedAt={positionsUpdatedAt}
                orders={orders}
                ordersError={ordersError}
                ordersLoading={!account}
                orderStatus={orderStatus}
                onOrderStatusChange={setOrderStatus}
                trades={trades}
                tradesError={tradesError}
                tradesLoading={!account}
                runs={runs}
                runsError={runsError}
                runsLoading={runs.length === 0 && !runsError}
                health={health}
                healthError={healthError}
              />
            )}
          </div>
        </>
      )}

      {view === "journal" && (
        <div className="p-3">
          <JournalPanel account={account} onOpenCandidate={openJournalCandidate} />
        </div>
      )}

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
