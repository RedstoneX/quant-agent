import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Button, Card } from "@tremor/react";
import {
  api,
  AccountResponse,
  HealthResponse,
  MacroBroaderContext,
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
import { LiquidityStrip } from "./components/LiquidityPanel";
import { HoldingsStrip } from "./components/HoldingsStrip";
import { DecisionStateBanner } from "./components/DecisionStateBanner";
import { TodaySessionsStrip } from "./components/TodaySessionsStrip";
import { CandidateRail } from "./components/CandidateRail";
import { PriceChartPanel } from "./components/PriceChartPanel";
import { PositionHoldingStrip } from "./components/PositionHoldingStrip";
import { DecisionSummaryLine } from "./components/DecisionSummaryLine";
import { PositionsPanel } from "./components/PositionsPanel";
import { SupportTabs } from "./components/SupportTabs";
import { DesktopCockpitWorkspace } from "./components/DesktopCockpitWorkspace";
import { SupportWorkspaceProvider } from "./context/SupportWorkspaceContext";
import { CockpitWorkspaceProvider } from "./context/CockpitWorkspaceContext";
import { useIsDesktop } from "./lib/useIsDesktop";
import { useOrderStatus } from "./components/OrdersPanel";
import { JournalPanel } from "./components/JournalPanel";
import { RunDetailModal } from "./components/RunDetailModal";
import { CandidateDetailModal } from "./components/CandidateDetailModal";
import { Pill } from "./components/ui/Pill";
import { bestPrimaryRunId } from "./components/funnelShared";
import { todayEtDate } from "./lib/format";
import { ResearchDesk } from "./components/research/ResearchDesk";

type View = "cockpit" | "desk" | "journal";
// "decision" removed (owner correction — the Decision Room panel is gone
// from the cockpit entirely; see PositionHoldingStrip/DecisionSummaryLine
// rendered inline under the chart pane instead, and PR description for
// where its content went).
type MobilePane = "positions" | "watchlist" | "chart";

/* Top-level view switcher — Cockpit (the live working surface) vs Journal
 * (the day-by-day narrative). Kept as two views rather than one more
 * panel in the cockpit stack: Journal is a full day's worth of reading on
 * its own and dilutes "what is happening right now" if it shares scroll
 * space with the cockpit. */
function ViewNav({
  view,
  onChange,
  trailing,
}: {
  view: View;
  onChange: (v: View) => void;
  /* Right-aligned slot (App.tsx's chrome-collapse control on the cockpit
   * view) — kept generic rather than a cockpit-specific prop so this bar
   * stays reusable for any future per-view control. */
  trailing?: ReactNode;
}) {
  return (
    <nav className="flex flex-wrap items-center gap-1 px-4 border-b border-border bg-bg">
      {(["cockpit", "desk", "journal"] as const).map((v) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          className={`px-3 py-2 text-[0.75rem] font-semibold uppercase tracking-wide border-b-2 -mb-px ${
            view === v ? "border-accent text-accent" : "border-transparent text-dim hover:text-ink"
          }`}
        >
          {v === "cockpit" ? "Cockpit" : v === "desk" ? "Research Desk" : "Journal"}
        </button>
      ))}
      {trailing && <div className="ml-auto">{trailing}</div>}
    </nav>
  );
}

const PANE_LABELS: Record<MobilePane, string> = {
  positions: "Positions",
  watchlist: "Candidates",
  chart: "Chart",
};

// Below the `xl` breakpoint (covers every iPad size, portrait or
// landscape) the three cockpit panes become an explicit tab strip instead
// of being squeezed side by side — a real tabbed surface, not a
// compressed desktop layout. Positions leads (item 1 of the cockpit
// trader rework): it's the first tab and the default landing pane on
// every breakpoint, matching the desktop Dockview layout below.
function PaneNav({ pane, onChange }: { pane: MobilePane; onChange: (p: MobilePane) => void }) {
  return (
    <div className="xl:hidden flex border-b border-border">
      {(["positions", "watchlist", "chart"] as const).map((p) => (
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
// only from the selected run's already-fetched funnel data (no extra
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
          {funnel ? "not among the selected run’s candidates" : "broad-market context — no session today yet"}
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
  // Open orders specifically — polled independently of the Orders panel's
  // own `orderStatus` display filter, so a stop-order lookup for the
  // charted/held symbol (chart's protective-stop line, the Decision
  // Room's holding card) stays correct even when the operator has that
  // filter set to "closed" or "all". See SupportWorkspaceContext.
  const [openOrders, setOpenOrders] = useState<OrderItem[]>([]);
  const [openOrdersError, setOpenOrdersError] = useState<string | null>(null);
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
  // currently drives Candidates/Chart, and `autoFollow`
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
  const todaysRequestIdRef = useRef(0);
  useEffect(() => {
    autoFollowRef.current = autoFollow;
    selectedRunIdRef.current = selectedRunId;
  }, [autoFollow, selectedRunId]);

  const funnel = selectedRunId ? todaysFunnels[selectedRunId] ?? null : null;
  const selectedSessionTrades = selectedRunId
    ? todaysTrades.filter((trade) => trade.run_id === selectedRunId)
    : [];

  // Item 7 (cockpit trader rework): "Market Regime" was a permanent empty
  // state whenever the currently SELECTED run happened to carry no macro
  // context — which includes every midday/close position-review session,
  // even on a day whose morning scan established a real regime a few
  // hours earlier. This looks across every one of today's already-fetched
  // funnels (not just the selected one) for the most recent one that
  // actually reported a regime, and carries its timestamp along as the
  // reading's age — so HeroBand can show "last known regime, N ago"
  // instead of manufacturing a false "no evidence" for a day that has
  // real evidence, just not on the selected run.
  const latestRegime = useMemo((): { macro: MacroBroaderContext; asOf: string | null } | null => {
    const withRegime = todaysRuns
      .filter((run) => todaysFunnels[run.run_id]?.macro_context?.regime)
      .sort((a, b) => (b.first_timestamp || "").localeCompare(a.first_timestamp || ""));
    const run = withRegime[0];
    if (!run) return null;
    const f = todaysFunnels[run.run_id];
    if (!f?.macro_context) return null;
    return { macro: f.macro_context, asOf: f.timestamp };
  }, [todaysRuns, todaysFunnels]);

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
  // truthfully no session data for the day, the Candidates side column
  // stops claiming the full viewport-locked height (which,
  // with nothing but a centered sentence to show, just produces a large
  // inert void) and instead collapse to their actual content height; the
  // price chart — the one column with genuine content in this state, real
  // SPY market context — claims the width that frees up. See the grid
  // below.
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
  // Mobile chart pane's inline holding strip (owner correction — no
  // modal/drawer, see PositionHoldingStrip). Desktop's Dockview ChartPane
  // derives the same thing from SupportWorkspace context directly.
  const chartHeldPosition = chartSymbol ? positions.find((p) => p.symbol === chartSymbol) : undefined;
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [view, setView] = useState<View>("cockpit");
  // Positions leads on every breakpoint (item 1) — the trader's first
  // question on arrival is "what do I hold," not "what did the scanner
  // find."
  const [mobilePane, setMobilePane] = useState<MobilePane>("positions");
  // Defaults compact: the price chart is the primary "answer at a glance"
  // surface and was reported cramped. HeroBand/TodaySessionsStrip/
  // DecisionStateBanner each already ship a `collapsed`/`compact` mode
  // (dense line instead of full cards/table) purpose-built to reclaim this
  // exact vertical space — see their own comments. Nothing here is
  // unreachable when collapsed: the same facts are still shown, just
  // denser, and the toggle below switches back to the full layout on
  // demand.
  const [chromeCompact, setChromeCompact] = useState(true);

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
      .orders("open")
      .then((r) => {
        setOpenOrders(r.orders);
        setOpenOrdersError(r.error);
      })
      .catch((err) => setOpenOrdersError(err.message));

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
    const requestId = ++todaysRequestIdRef.current;
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
          if (requestId !== todaysRequestIdRef.current) return;
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
            selectedRunIdRef.current = best;
            setSelectedRunId(best);
          }
        })
      )
      .catch((err) => {
        if (requestId !== todaysRequestIdRef.current) return;
        if (err.status === 404) {
          setTodaysRuns([]);
          setTodaysFunnels({});
          setTodaysTrades([]);
          setTodaysError(null);
          setTodaysLoading(false);
          setTodaysUpdatedAt(new Date());
          if (autoFollowRef.current) {
            selectedRunIdRef.current = null;
            setSelectedRunId(null);
          }
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
    autoFollowRef.current = false;
    selectedRunIdRef.current = runId;
    setAutoFollow(false);
    setSelectedRunId(runId);
  }

  function followPrimarySession() {
    const best = bestPrimaryRunId(todaysRuns, todaysFunnels);
    autoFollowRef.current = true;
    selectedRunIdRef.current = best;
    setAutoFollow(true);
    setSelectedRunId(best);
  }

  function selectSessionTrade(trade: TradeItem) {
    // A trade row is scoped to the currently selected run. Make that
    // selection explicitly manual before changing the chart so the next
    // poll cannot auto-promote a different session out from under it.
    if (selectedRunId) selectSession(selectedRunId);
    setChartSymbol(trade.symbol);
    setMobilePane("chart");
  }

  function inspectSymbol(symbol: string) {
    setChartSymbol(symbol);
    setMobilePane("chart");
    if (funnel?.candidates.some((candidate) => candidate.symbol === symbol)) {
      modalActions.openCandidateDetail(funnel.run_id, symbol);
    }
  }

  // Position-panel-specific: chart the symbol so PositionHoldingStrip picks
  // it up inline under the chart (it already reads chartSymbol/positions)
  // — and nothing else. Deliberately never opens the candidate-detail
  // modal, unlike inspectSymbol above: clicking a holding answers "what is
  // my position?", not "what did this candidate do in some run?", and no
  // popup/modal/dialog/drawer may ever cover the chart on a position
  // click (cockpit trader rework, item 2/3 as corrected by the owner).
  function chartPositionSymbol(symbol: string) {
    setChartSymbol(symbol);
    setMobilePane("chart");
  }

  function inspectTrade(trade: TradeItem) {
    setChartSymbol(trade.symbol);
    setMobilePane("chart");
    if (trade.run_id) {
      if (todaysRuns.some((run) => run.run_id === trade.run_id)) selectSession(trade.run_id);
      modalActions.openCandidateDetail(trade.run_id, trade.symbol);
    }
  }

  function inspectOrder(order: OrderItem) {
    const linkedTrade = trades.find((trade) => trade.broker_order_id === order.id);
    if (linkedTrade) inspectTrade(linkedTrade);
    else inspectSymbol(order.symbol);
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
        <ViewNav
          view={view}
          onChange={setView}
          trailing={
            view === "cockpit" ? (
              <button
                type="button"
                onClick={() => setChromeCompact((v) => !v)}
                className="px-3 py-2 text-[0.75rem] font-semibold uppercase tracking-wide text-dim hover:text-ink"
                aria-pressed={chromeCompact}
              >
                {chromeCompact ? "Show full header" : "Compact header"}
              </button>
            ) : undefined
          }
        />

        {view === "cockpit" && (
          <>
            {/* Item 6 (cockpit trader rework): holdings and P&L lead —
                the first question a trader asks on arrival — with the
                portfolio abstractions (NLV card, exposure gauge, regime)
                demoted below as compact, secondary chrome. Reuses the same
                broker-marked positions state HeroBand/PositionsPanel
                already render; a click charts the symbol in place, no
                modal (item 2/3 — see chartPositionSymbol). */}
            <HoldingsStrip positions={positions} error={positionsError} updatedAt={positionsUpdatedAt} onSelectSymbol={chartPositionSymbol} />
            <HeroBand account={account} accountError={accountError} positions={positions} regime={latestRegime} collapsed={chromeCompact} />
            {/* Item 9: six liquidity stat tiles condensed to one compact
                row, and moved out of the workspace tab strip entirely —
                secondary portfolio-abstraction chrome, same spirit as
                HeroBand's own demotion (item 6). */}
            <LiquidityStrip account={account} accountError={accountError} positions={positions} />
            <TodaySessionsStrip
              runs={todaysRuns}
              funnels={todaysFunnels}
              trades={todaysTrades}
              loading={todaysLoading}
              error={todaysError}
              selectedRunId={selectedRunId}
              autoFollow={autoFollow}
              onSelect={selectSession}
              onFollowLatest={followPrimarySession}
              onSelectTrade={selectSessionTrade}
            />
            <DecisionStateBanner funnel={funnel} trades={todaysTrades} loading={todaysLoading} error={todaysError} updatedAt={todaysUpdatedAt} compact={chromeCompact} />
          </>
        )}
      </div>

      {view === "cockpit" && (
        <>
          {isDesktop ? (
            <SupportWorkspaceProvider value={{
              account, accountError, positions, positionsError,
              positionsLoading: !account && !positionsError, positionsUpdatedAt,
              orders, ordersError, ordersLoading: !account, orderStatus,
              onOrderStatusChange: setOrderStatus, openOrders, trades, tradesError,
              tradesLoading: !account, runs, runsError,
              runsLoading: runs.length === 0 && !runsError, health, healthError,
              onSelectPositionSymbol: chartPositionSymbol,
              onInspectOrder: inspectOrder, onInspectTrade: inspectTrade,
            }}>
              <CockpitWorkspaceProvider value={{
                funnel, loading: todaysLoading, error: todaysError,
                updatedAt: todaysUpdatedAt, chartSymbol,
                chartTrades: selectedSessionTrades, onSelectSymbol: setChartSymbol,
              }}>
                <DesktopCockpitWorkspace />
              </CockpitWorkspaceProvider>
            </SupportWorkspaceProvider>
          ) : (
            <>
              <PaneNav pane={mobilePane} onChange={setMobilePane} />
              <div className="p-3">
                {mobilePane === "positions" && <PositionsPanel positions={positions} error={positionsError} loading={!account && !positionsError} updatedAt={positionsUpdatedAt} onSelectSymbol={chartPositionSymbol} />}
                {mobilePane === "watchlist" && <CandidateRail funnel={funnel} loading={todaysLoading} error={todaysError} updatedAt={todaysUpdatedAt} selectedSymbol={chartSymbol} onSelectSymbol={setChartSymbol} />}
                {mobilePane === "chart" && (
                  <div className="flex min-h-[520px] flex-col gap-2">
                    <SelectedSymbolContext funnel={funnel} symbol={chartSymbol} onOpenDetail={() => chartSymbol && funnel && modalActions.openCandidateDetail(funnel.run_id, chartSymbol)} />
                    {chartHeldPosition && <PositionHoldingStrip position={chartHeldPosition} openOrders={openOrders} trades={trades} />}
                    <DecisionSummaryLine funnel={funnel} symbol={chartSymbol} />
                    <div className="min-h-0 flex-1"><PriceChartPanel symbol={chartSymbol} trades={selectedSessionTrades} positionTrades={trades} positions={positions} openOrders={openOrders} /></div>
                  </div>
                )}
              </div>
              <div className="px-3 pb-3">
              <SupportTabs
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
                onSelectPositionSymbol={chartPositionSymbol}
                onInspectOrder={inspectOrder}
                onInspectTrade={inspectTrade}
              />
              </div>
            </>
          )}
        </>
      )}

      {view === "journal" && (
        <div className="p-3">
          <JournalPanel account={account} onOpenCandidate={openJournalCandidate} />
        </div>
      )}

      {view === "desk" && <ResearchDesk />}

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
