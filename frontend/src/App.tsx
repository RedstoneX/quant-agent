import { useEffect, useRef, useState } from "react";
import { Button, Card } from "@tremor/react";
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
import { TodaySessionsStrip } from "./components/TodaySessionsStrip";
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
import { bestPrimaryRunId } from "./components/funnelShared";
import { todayEtDate } from "./lib/format";

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
          className={`px-3 py-2 text-[0.75rem] font-semibold uppercase tracking-wide border-b-2 -mb-px ${
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
          className={`flex-1 py-2 text-[0.75rem] font-semibold uppercase tracking-wide border-b-2 ${
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
    <Card className="mb-3 flex !w-auto flex-wrap items-center gap-2 !bg-panel-alt !p-2.5 !ring-border">
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
          <Button type="button" variant="light" size="xs" color="cyan" onClick={onOpenDetail} className="ml-auto">
            Full drill-down &rarr;
          </Button>
        </>
      ) : (
        <span className="text-dim text-[0.8125rem]">
          {funnel ? "not among the latest run’s candidates" : "broad-market context — no session today yet"}
        </span>
      )}
    </Card>
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

  // Day-scoped session state — replaces a single "latest run" funnel.
  // QAMC runs several session types a day, several of which (midday/close
  // position review) structurally carry zero candidates; always following
  // the literal latest run means a routine afternoon review silently
  // blanks the cockpit even when a real morning scan ran. `todaysRuns`/
  // `todaysFunnels` cover every one of today's runs (reusing the journal
  // endpoints' existing ET-trading-day grouping — see
  // docs/architecture/MISSION_CONTROL_API.md's Stage 5 entry — rather than
  // re-implementing date bucketing here), `selectedRunId` is whichever one
  // currently drives Candidates/Chart/Decision Room, and `autoFollow`
  // tracks whether that selection is still automatic (best-primary-run,
  // see funnelShared.ts::bestPrimaryRunId) or the operator pinned one via
  // TodaySessionsStrip.
  const [todaysRuns, setTodaysRuns] = useState<RunSummary[]>([]);
  const [todaysFunnels, setTodaysFunnels] = useState<Record<string, RunFunnelResponse | null>>({});
  const [todaysTrades, setTodaysTrades] = useState<TradeItem[]>([]);
  const [todaysError, setTodaysError] = useState<string | null>(null);
  const [todaysLoading, setTodaysLoading] = useState(true);
  const [todaysUpdatedAt, setTodaysUpdatedAt] = useState<Date | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  // usePoll(fn, []) freezes `fn`'s closure at mount — reading component
  // state directly inside it would always see the initial value (a real,
  // separate bug this same change fixes for chartSymbol below). Refs give
  // the poll callback a way to read the CURRENT autoFollow/selectedRunId.
  const autoFollowRef = useRef(autoFollow);
  const selectedRunIdRef = useRef(selectedRunId);
  useEffect(() => {
    autoFollowRef.current = autoFollow;
    selectedRunIdRef.current = selectedRunId;
  }, [autoFollow, selectedRunId]);

  const funnel = selectedRunId ? todaysFunnels[selectedRunId] ?? null : null;

  // Fix 1 (visual convergence plan §2.2, Finding D): the primary 3-column
  // row below claims a fixed viewport-bounded height so it's a real
  // "answer at a glance" workstation rather than an unboundedly tall page
  // — but the height BUDGET for that row is "100vh minus everything above
  // it," and everything above it (TopStrip + ViewNav + HeroBand +
  // TodaySessionsStrip + DecisionStateBanner) is genuinely variable height:
  // TodaySessionsStrip renders null with zero sessions, DecisionStateBanner
  // wraps to 1-2 lines depending on content, a stale-data warning row can
  // appear/disappear. A single hardcoded constant drifts every time one of
  // those rows changes shape — exactly how the previous "150px" constant
  // went stale (real measured chrome was 423px, not 150px). Measuring it
  // live via ResizeObserver and writing it to the --chrome-h CSS custom
  // property (see styles/index.css's :root) keeps the row's declared
  // height honest without forcing a React re-render on every resize tick.
  const chromeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = chromeRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const h = entries[0]?.contentRect.height;
      // The grid's own border-box extends past its columns' bottom edge by
      // its own bottom `p-3` padding (12px) in addition to the 12px top
      // padding this wrapper's bottom edge already sits above — 24px
      // total — folded in here so the CSS calc() stays a simple two-term
      // `100vh - var(--chrome-h)`, the same shape as the constant it
      // replaces. Confirmed via measurement: without the +24, the grid's
      // own getBoundingClientRect().bottom lands 12px past the viewport
      // (its padding-bottom past the fold) even though every column's
      // actual content is fully reachable.
      if (h) document.documentElement.style.setProperty("--chrome-h", `${Math.ceil(h) + 24}px`);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Fix 3 (visual convergence plan §2.4, Finding C): when there is
  // truthfully no session data for the day, the Candidates/Decision Room
  // side columns stop claiming the full viewport-locked height (which,
  // with nothing but a centered sentence to show, just produces a large
  // inert void) and instead collapse to their actual content height; the
  // price chart — the one column with genuine content in this state, real
  // SPY market context — claims the width that frees up. See the grid
  // below.
  const sparseDay = todaysRuns.length === 0;

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsError, setRunsError] = useState<string | null>(null);
  // Defaults to the broad market rather than null: an unselected chart
  // previously rendered as a large blank panel with a "click a candidate"
  // hint floating in it — dead space in the highest-visual-weight column
  // of the primary cockpit. SPY is always real, always liquid, and gives
  // chart-led MARKET context (docs/OUTCOME.md) even before any candidate
  // exists; a real per-run candidate always overrides it once one exists
  // (see the selectedRunId effect below).
  const [chartSymbol, setChartSymbol] = useState<string | null>("SPY");
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
  }, []);

  // Day-scoped session poll — every one of today's runs (via the journal
  // endpoints' existing ET-trading-day grouping) plus each one's funnel,
  // in parallel. A single failed run's funnel fetch is caught to `null`
  // rather than aborting the whole batch (same resilience contract
  // JournalPanel already established). Auto-follow recomputes the best
  // primary run each tick unless the operator has pinned a different one
  // via TodaySessionsStrip.
  usePoll(() => {
    // Deliberately today's literal ET calendar date, NOT journalDates(1)
    // (the most recent day the journal listing considers "complete" —
    // JournalPanel's own default view). During market hours today has
    // real runs long before it gets a daily_pnl/close snapshot, so
    // journalDates(1) would keep resolving to YESTERDAY all day — the
    // exact same erasure this strip exists to fix, just relabeled
    // "Today's sessions" while actually showing a stale prior day. A 404
    // (no runs recorded yet today — before the first session, or a
    // non-trading day) is an honest empty state, not an error.
    api
      .journalDay(todayEtDate())
      .then((day) =>
        Promise.all(
          day.runs.map((r) =>
            api
              .runFunnel(r.run_id)
              .then((f): [string, RunFunnelResponse | null] => [r.run_id, f])
              .catch((): [string, RunFunnelResponse | null] => [r.run_id, null])
          )
        ).then((pairs) => {
          const funnels = Object.fromEntries(pairs);
          setTodaysRuns(day.runs);
          setTodaysFunnels(funnels);
          setTodaysTrades(day.trades);
          setTodaysError(null);
          setTodaysLoading(false);
          setTodaysUpdatedAt(new Date());

          const best = bestPrimaryRunId(day.runs, funnels);
          const stillExists = selectedRunIdRef.current && day.runs.some((r) => r.run_id === selectedRunIdRef.current);
          if (autoFollowRef.current || !stillExists) {
            setSelectedRunId(best);
          }
        })
      )
      .catch((err) => {
        if (err.status === 404) {
          setTodaysRuns([]);
          setTodaysFunnels({});
          setTodaysTrades([]);
          setTodaysError(null);
          setTodaysLoading(false);
          setTodaysUpdatedAt(new Date());
          if (autoFollowRef.current) setSelectedRunId(null);
          return;
        }
        // Deliberately does NOT clear todaysRuns/todaysFunnels — the
        // strip/rail/decision-room render the retained data tagged stale
        // instead of blanking real information on a transient poll error.
        setTodaysError(err.message);
        setTodaysLoading(false);
      });
  }, []);

  // Re-chart whenever the SELECTED run changes (auto-follow promoting a
  // different primary run, or an operator's manual pick) — a normal effect
  // with `selectedRunId` in its deps, not logic inside the poll callback
  // above, which would suffer the same frozen-closure problem the prior
  // `if (!chartSymbol && ...)` line actually had: usePoll(fn, []) captures
  // `fn` once at mount, so a read of component state inside it is always
  // the value from that first render — every poll tick was unconditionally
  // resetting chartSymbol back to the funnel's first candidate, silently
  // overriding any candidate the operator had clicked to chart, every 20s.
  useEffect(() => {
    if (!selectedRunId) return;
    const f = todaysFunnels[selectedRunId];
    setChartSymbol(f && f.candidates.length ? f.candidates[0].symbol : "SPY");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRunId]);

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

  function selectSession(runId: string) {
    setAutoFollow(false);
    setSelectedRunId(runId);
  }

  function followLatestSession() {
    setAutoFollow(true);
    setSelectedRunId(bestPrimaryRunId(todaysRuns, todaysFunnels));
  }

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
      {/* Fix 1: chromeRef wraps exactly the header stack whose real height
          drives the primary row's viewport budget below (TopStrip through
          DecisionStateBanner) — see the ResizeObserver effect above. A bare
          div with no padding/border/margin is transparent to layout and
          does not interfere with TopStrip's own `sticky` positioning. */}
      <div ref={chromeRef}>
        <TopStrip account={account} accountError={accountError} health={health} updatedAt={updatedAt} />
        <ViewNav view={view} onChange={setView} />

        {view === "cockpit" && (
          <>
            <HeroBand account={account} accountError={accountError} positions={positions} funnel={funnel} />
            <TodaySessionsStrip
              runs={todaysRuns}
              funnels={todaysFunnels}
              trades={todaysTrades}
              loading={todaysLoading}
              error={todaysError}
              selectedRunId={selectedRunId}
              autoFollow={autoFollow}
              onSelect={selectSession}
              onFollowLatest={followLatestSession}
            />
            <DecisionStateBanner funnel={funnel} trades={todaysTrades} loading={todaysLoading} error={todaysError} updatedAt={todaysUpdatedAt} />
          </>
        )}
      </div>

      {view === "cockpit" && (
        <>
          <PaneNav pane={mobilePane} onChange={setMobilePane} />

          {/* The primary cockpit body — deliberately a fixed, non-dockable
              grid (not Dockview): this is the "answer at a glance" surface,
              customization belongs only to the support workspace below. */}
          {/* All three columns share one explicit height at desktop width,
              not just a max-height, so the row is a real viewport-bounded
              workstation, not just capped — Candidates/Decision Room
              scroll internally within it, and the center column can flex
              its own children to actually fill it (see the chart's flex-1
              wrapper below). The height itself is `100vh` minus
              `--chrome-h`, a CSS custom property kept live by chromeRef's
              ResizeObserver above (Fix 1) rather than a hardcoded constant
              — the previous flat "150px" drifted out of sync with the
              header stack's real (variable) height, silently pushing the
              highest-stakes part of the Decision Room chain below the
              fold.
              Fix 3 (Finding C): on a genuinely no-session day, the two
              side columns don't have a viewport-locked height to fill in
              the first place (`sparseDay` below), so they collapse to
              their actual (small) content height via `self-start` instead
              of Grid's default stretch-to-row-height, and the grid's own
              column template narrows to give the chart — the one column
              with genuine content in this state, real SPY market context —
              the freed width. The chart column keeps its full viewport
              height in both states; only the two side columns' height
              behavior changes. */}
          <div
            className={`grid grid-cols-1 ${
              sparseDay ? "xl:grid-cols-[260px_1fr_260px]" : "xl:grid-cols-[300px_1fr_360px]"
            } gap-3 p-3 items-stretch`}
          >
            <div
              className={`${mobilePane === "watchlist" ? "block" : "hidden xl:block"} ${
                sparseDay ? "xl:self-start" : "xl:h-[calc(100vh_-_var(--chrome-h))] xl:overflow-y-auto"
              }`}
            >
              <CandidateRail
                funnel={funnel}
                loading={todaysLoading}
                error={todaysError}
                updatedAt={todaysUpdatedAt}
                selectedSymbol={chartSymbol}
                onSelectSymbol={setChartSymbol}
              />
            </div>

            {/* min-w-0 is load-bearing: a CSS Grid `1fr` track defaults to
                minmax(auto, 1fr), so without it this column would size to
                its widest child's natural content width (the price chart)
                instead of shrinking to its assigned share of the row,
                pushing the Decision Room column past the viewport edge. */}
            <div
              className={`${
                mobilePane === "chart" ? "flex" : "hidden xl:flex"
              } min-w-0 flex-col xl:h-[calc(100vh_-_var(--chrome-h))]`}
            >
              <SelectedSymbolContext
                funnel={funnel}
                symbol={chartSymbol}
                onOpenDetail={() => chartSymbol && funnel && modalActions.openCandidateDetail(funnel.run_id, chartSymbol)}
              />
              {/* flex-1 + min-h-0 is the other half of the fix: without
                  min-h-0 a flex child's default min-height:auto lets its
                  content (the chart) refuse to shrink below its own natural
                  size, which would silently defeat the resize-to-fill
                  behavior on a short viewport. */}
              <div className="flex-1 min-h-0">
                <PriceChartPanel symbol={chartSymbol} trades={trades} />
              </div>
            </div>

            <div
              className={`${mobilePane === "decision" ? "block" : "hidden xl:block"} ${
                sparseDay ? "xl:self-start" : "xl:h-[calc(100vh_-_var(--chrome-h))] xl:overflow-y-auto"
              }`}
            >
              <DecisionRoomPanel funnel={funnel} symbol={chartSymbol} loading={todaysLoading} error={todaysError} updatedAt={todaysUpdatedAt} />
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
