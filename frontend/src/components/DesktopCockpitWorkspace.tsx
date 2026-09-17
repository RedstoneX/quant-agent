import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Text } from "@tremor/react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useCockpitWorkspace } from "../context/CockpitWorkspaceContext";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { useModalActions } from "../context/ModalContext";
import {
  COCKPIT_BOTTOM_ROW_DEFAULT_HEIGHT,
  COCKPIT_BOTTOM_ROW_MIN_HEIGHT,
  COCKPIT_LAYOUT_STORAGE_KEY,
  sashDragGrowthPx,
  sashDragShouldGrowWorkspace,
} from "./cockpitWorkspaceLayout";
import { CandidateRail } from "./CandidateRail";
import { PriceChartPanel } from "./PriceChartPanel";
import { PositionHoldingStrip } from "./PositionHoldingStrip";
import { DecisionSummaryLine } from "./DecisionSummaryLine";
import { PositionsPanel } from "./PositionsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { TradesPanel } from "./TradesPanel";
import { RunsPanel } from "./RunsPanel";
import { DirectionalBiasPanel } from "./DirectionalBiasPanel";
import { MissedOpportunitiesPanel } from "./MissedOpportunitiesPanel";
import { SearchPanel } from "./SearchPanel";
import { HealthPanel } from "./HealthPanel";
import { Pill } from "./ui/Pill";
import { Panel, StateMessage } from "./ui/Panel";

// Item 2 of cockpit pass 3: the middle column of the bottom row used to
// be deliberately empty by default — "free for him to populate" in the
// owner's own words, not a panel we picked for him. Built from the same
// approved Panel/StateMessage primitives every other pane in this
// workspace uses (no bespoke graphic), reusing the existing ●/◐/■/○
// glyph vocabulary (see ui/Panel.tsx's StateMessage) rather than
// inventing a new icon for "nothing here yet."
//
// v6 update: the owner asked to get that column's width back for
// Positions/Orders instead, so buildDefaultLayout no longer places this
// panel — see the STORAGE_KEY comment below. The component stays defined
// and registered in COMPONENTS regardless: a saved layout can still carry
// a "workspaceSlot" entry (e.g. one written under this key by an earlier
// build before the code caught up, or before a future default change),
// and dockview's fromJSON throws on a panel id with no registered
// component. Keeping this around is what makes loading such a layout safe
// rather than a hard failure. It is retained purely for that
// compatibility — it is not placed by default any more.
function WorkspaceSlotPane() {
  return (
    <div className="h-full overflow-y-auto p-2">
      <Panel title="Workspace">
        <StateMessage
          hero
          glyph="○"
          text="Empty by default — drag any panel's tab here to fill this column, or use “Reset layout” to restore the default."
        />
      </Panel>
    </div>
  );
}

// Item 1 of the cockpit trader rework: Positions is the panel a trader
// lands on, not one tab among several — see PositionsPane below and
// buildDefaultLayout's placement of it as the leftmost, active-by-default
// group.
function PositionsPane() {
  const state = useSupportWorkspace();
  return (
    <div className="h-full overflow-y-auto p-2">
      <PositionsPanel
        positions={state.positions}
        error={state.positionsError}
        loading={state.positionsLoading}
        updatedAt={state.positionsUpdatedAt}
        onSelectSymbol={state.onSelectPositionSymbol}
      />
    </div>
  );
}

function CandidatesPane() {
  const state = useCockpitWorkspace();
  return <div className="h-full overflow-y-auto p-2"><CandidateRail funnel={state.funnel} loading={state.loading} error={state.error} updatedAt={state.updatedAt} selectedSymbol={state.chartSymbol} onSelectSymbol={state.onSelectSymbol} /></div>;
}

// Owner correction: the Decision Room panel is gone (see PR description).
// What used to be its "position I hold" answer is now
// PositionHoldingStrip — an inline compact strip under the chart, never a
// popup/modal/drawer — and its "what did this run's candidate do" answer
// is DecisionSummaryLine, a single line that renders nothing at all
// unless there is real content. Both sit directly under the chart, in the
// same pane, so nothing ever covers the candles.
function ChartPane() {
  const state = useCockpitWorkspace();
  // Read-only broker positions/orders/trades, sourced from the same
  // SupportWorkspace state PositionsPane/OrdersPane render — needed so the
  // chart's average-entry (entryPriceLine) and protective-stop
  // (positionStopLine) reference lines actually have data to draw from in
  // the desktop Dockview workspace, not just the mobile/iPad pane.
  const support = useSupportWorkspace();
  const { openCandidateDetail } = useModalActions();
  const candidate = state.funnel?.candidates.find((item) => item.symbol === state.chartSymbol);
  const heldPosition = state.chartSymbol ? support.positions.find((p) => p.symbol === state.chartSymbol) : undefined;
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden px-1.5 py-1 gap-1">
      <div className="flex min-w-0 flex-shrink-0 flex-wrap items-center gap-x-2 gap-y-1">
          {state.previousChartSymbol && state.previousChartSymbol !== state.chartSymbol && state.onGoBackSymbol && (
            <Button
              type="button"
              variant="secondary"
              size="xs"
              color="cyan"
              onClick={state.onGoBackSymbol}
              title={`Back to ${state.previousChartSymbol}`}
              aria-label={`Back to ${state.previousChartSymbol}`}
            >
              &larr; {state.previousChartSymbol}
            </Button>
          )}
          <span className="font-bold">{state.chartSymbol || "Market"}</span>
          {candidate && <><Pill text={candidate.direction} /><Pill text={candidate.order_status || (candidate.executed ? "executed" : "not executed")} /></>}
          {!candidate && <Text>Market context; no selected-run candidate evidence.</Text>}
          {candidate && state.funnel && (
            <Button className="ml-auto" size="xs" variant="light" color="cyan" onClick={() => openCandidateDetail(state.funnel!.run_id, candidate.symbol)}>Lifecycle &rarr;</Button>
          )}
      </div>
      {heldPosition && (
        <PositionHoldingStrip position={heldPosition} openOrders={support.openOrders} trades={support.trades} />
      )}
      <DecisionSummaryLine funnel={state.funnel} symbol={state.chartSymbol} />
      {/* Fix (owner UI pass, item 4 "chart internal scrollbar"): this used
          to be `overflow-y-auto` on the column above, which is what
          actually produced the ugly internal scrollbar — the chart's own
          `h-full` (see PriceChartPanel.tsx) was fighting the header
          Card/PositionHoldingStrip/DecisionSummaryLine above it for a
          share of this dockview panel's real height, and overflow-y-auto
          papered over that fight with a scrollbar instead of letting the
          chart actually shrink to fit. `overflow-hidden` above plus
          `min-h-0` here (so this flex item can shrink below its content
          size instead of forcing the column to overflow) is what makes the
          chart genuinely responsive to the panel's real height now — see
          PriceChartPanel.tsx's own flex-column restructuring of its
          chart-area/slider split for the other half of this fix. */}
      <div className="min-h-0 flex-1">
        <PriceChartPanel
          symbol={state.chartSymbol}
          trades={state.chartTrades}
          positions={support.positions}
          openOrders={support.openOrders}
          positionTrades={support.trades}
          onUserInteraction={state.onChartInteraction}
        />
      </div>
    </div>
  );
}

function OrdersPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><OrdersPanel orders={state.orders} error={state.ordersError} loading={state.ordersLoading} status={state.orderStatus} onStatusChange={state.onOrderStatusChange} onInspect={state.onInspectOrder} onSelectSymbol={state.onSelectPositionSymbol} /></div>;
}

function TradesPane() {
  const state = useSupportWorkspace();
  return <div className="h-full overflow-y-auto p-2"><TradesPanel trades={state.trades} error={state.tradesError} loading={state.tradesLoading} onInspect={state.onInspectTrade} onSelectSymbol={state.onSelectPositionSymbol} /></div>;
}

function RunsPane() { const state = useSupportWorkspace(); return <div className="h-full overflow-y-auto p-2"><RunsPanel runs={state.runs} error={state.runsError} loading={state.runsLoading} /></div>; }
function BiasPane() { return <div className="h-full overflow-y-auto p-2"><DirectionalBiasPanel /></div>; }
// Was wired to a callback that conditionally opened the candidate-detail
// modal depending on which session happened to be selected in the
// Sessions strip — unrelated to the missed-opportunity row being clicked
// — so the identical click did two different things with no on-screen
// explanation. Routed to the same modal-free callback PositionsPane uses
// above: chart the symbol, open nothing (governing principle, App.tsx's
// chartPositionSymbol).
function MissedPane() { const state = useSupportWorkspace(); return <div className="h-full overflow-y-auto p-2"><MissedOpportunitiesPanel onSelectSymbol={state.onSelectPositionSymbol} /></div>; }

// Item 13 (cockpit trader rework): System and Search — named by the owner
// as "not trading" — used to each be their own top-level tab in this
// workspace's chart-group tab strip. Folded into one Diagnostics tab
// instead of standing on their own; same two panels, just one click away
// together rather than two clicks apart.
function DiagnosticsPane() {
  const state = useSupportWorkspace();
  return (
    <div className="h-full overflow-y-auto p-2 flex flex-col gap-3">
      <HealthPanel health={state.health} error={state.healthError} />
      <SearchPanel onSelectSymbol={state.onSelectPositionSymbol} />
    </div>
  );
}

const COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  positions: PositionsPane,
  candidates: CandidatesPane,
  chart: ChartPane,
  orders: OrdersPane,
  trades: TradesPane,
  runs: RunsPane,
  bias: BiasPane,
  missed: MissedPane,
  diagnostics: DiagnosticsPane,
  workspaceSlot: WorkspaceSlotPane,
};

// Bumped v6 -> v7: chart is the primary stage. v6 saved a tall Positions/
// Orders row (340px default, 260px floor) that left the candles as a
// thin strip; this key is not migrated so every browser picks up the
// shorter blotter default. A v6 blob is still a valid dockview shape —
// we just stop looking it up, same as every previous bump.
//
// Bumped v5 -> v6 (owner request, direct): the bottom row's middle
// "Workspace" slot — genuinely empty by default, see WorkspaceSlotPane
// above — was eating roughly a third of the bottom row's width while
// showing nothing. The owner asked for a plain two-column bottom row
// instead (Positions | Orders, split evenly) so those two panels get that
// width back; buildDefaultLayout no longer adds a workspaceSlot panel at
// all. As with every previous bump, a v5 layout persisted from
// localStorage is not being migrated or read under this key — it simply
// stops being looked up, which is the point: without bumping the key,
// every browser that already has a saved v5 layout (three columns, empty
// middle) would keep opening to that shape and never see the new
// two-column default. WorkspaceSlotPane's component registration is kept
// regardless (see its comment above) purely so a layout blob that does
// reference "workspaceSlot" can still be restored via fromJSON without
// throwing on an unknown component id.
//
// Bumped v4 -> v5 (cockpit pass 3, item 2): the default workspace shape
// changed from one row of three columns (Positions | Chart | Orders) to
// two rows — a full-width Chart row on top, then a Positions/free/Orders
// three-column row below (see buildDefaultLayout). A v4 layout persisted
// from localStorage is still a perfectly valid dockview shape (nothing
// referenced by it was removed), so this bump exists purely to change
// what EVERY BROWSER sees as the default on first load / after "Reset
// layout" — without it, anyone who already has a saved v4 layout would
// keep seeing the old one-row shape and never notice the new default
// exists. Existing saved layouts are otherwise untouched: dockview state
// is genuinely additive/positional, not a fixed schema, so a v4 blob
// would have loaded fine under this key too. (v3 -> v4 / v2 -> v3
// history: the Decision Room panel was removed entirely — its content
// moved inline under the chart as PositionHoldingStrip/DecisionSummaryLine
// or already exists on the Research Desk as DecisionDeltaPanel; Positions
// moved from a background tab inside the chart group to its own leftmost,
// active-by-default group; Liquidity left the workspace entirely for a
// compact header row — item 9; System+Search merged into one Diagnostics
// panel — item 13.)
const STORAGE_KEY = COCKPIT_LAYOUT_STORAGE_KEY;

// Item 2 of cockpit pass 3, revised per direct owner request ("a better
// DEFAULT, not a lock" — every panel below stays exactly as
// movable/dockable/resizable as it always was; this only changes what a
// brand-new session (or "Reset layout") starts from). Originally: chart
// alone across the top, then a three-column row underneath with a
// genuinely empty middle column for the owner to fill himself. He later
// asked for that middle column back as width for Positions and Orders
// instead — chart full width on top because it's the first thing he
// looks at, then just two columns underneath, Positions left and Orders
// right, split evenly so each gets real horizontal room. He can still
// rearrange everything himself via the tabs; this only changes the
// starting shape.
function buildDefaultLayout(api: DockviewApi) {
  // Top row: the chart, full width — nothing else in this row.
  api.addPanel({ id: "chart", component: "chart", title: "Chart" });
  // The non-trading analysis panels ride along as background tabs on the
  // chart group (unchanged from before this pass) rather than moving to
  // the bottom row — they pair conceptually with "studying a symbol/run",
  // not with the Positions/Orders tables.
  for (const [id, title] of [["missed", "Missed"], ["runs", "Runs"], ["bias", "Directional Bias"], ["diagnostics", "Diagnostics"]]) {
    api.addPanel({ id, component: id, title, position: { referencePanel: "chart", direction: "within" }, inactive: true });
  }

  // Bottom row: Positions (left) | Orders (right) — a real second row
  // (direction: "below"), not more tabs folded into the chart group, so
  // it gets its own genuine height AND its own independent resize handle
  // against the chart row above it — this is dockview's own native
  // sibling-panel resize sash (--dv-sash-color/--dv-active-sash-color in
  // styles/index.css), not a manual CSS hack; dragging the thin bar
  // between the chart row and this row resizes both, with a proper
  // resize cursor and highlight on hover, exactly like any other dockview
  // split. Two columns only, no fixed pixel width on either side: leaving
  // both without an initialWidth lets dockview split the row evenly
  // between them.
  //
  // Chart is the primary stage. The bottom blotter starts short so the
  // candles and levels get the majority of the workspace; operators who
  // want more blotter rows drag the sash (that choice persists). Floor is
  // dockview's native minimumHeight so the row cannot be crushed to a
  // title bar. Constants live in cockpitWorkspaceLayout.ts.
  api.addPanel({
    id: "positions",
    component: "positions",
    title: "Positions",
    position: { referencePanel: "chart", direction: "below" },
    initialHeight: COCKPIT_BOTTOM_ROW_DEFAULT_HEIGHT,
    minimumHeight: COCKPIT_BOTTOM_ROW_MIN_HEIGHT,
  });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  api.addPanel({ id: "orders", component: "orders", title: "Orders", position: { referencePanel: "positions", direction: "right" }, minimumHeight: COCKPIT_BOTTOM_ROW_MIN_HEIGHT });
  api.addPanel({ id: "trades", component: "trades", title: "Trades", position: { referencePanel: "orders", direction: "within" }, inactive: true });

  api.getPanel("positions")?.api.setActive();
}

// Dockview only redistributes space inside a definite-height box; it
// cannot grow that box. Once Positions/Orders sit on minimumHeight, a
// further downward sash drag has nowhere to take height from unless this
// wrapper grows. Growing on EVERY downward drag (the previous behaviour)
// races Dockview's saveProportions + ResizeObserver and snaps the chart
// back on mouse-up. Growth therefore starts only after the bottom row is
// already on its floor; in-box resizes are left to Dockview itself.
const WORKSPACE_DEFAULT_HEIGHT = "max(760px, calc(100vh - var(--chrome-h) + 32px))";

function applyWorkspaceHeight(wrapper: HTMLElement, growthPx: number) {
  wrapper.style.height = growthPx > 0 ? `calc(${WORKSPACE_DEFAULT_HEIGHT} + ${growthPx}px)` : "";
}

function pinChartHeight(api: DockviewApi | null, height: number) {
  const chartGroup = api?.getPanel("chart")?.group;
  if (!chartGroup || height <= 0) return;
  chartGroup.api.setSize({ height });
}

export function DesktopCockpitWorkspace() {
  const apiRef = useRef<DockviewApi | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  // Extra pixels grown on top of WORKSPACE_DEFAULT_HEIGHT — see the block
  // comment above WORKSPACE_DEFAULT_HEIGHT for the mechanism. Deliberately
  // NOT persisted to localStorage: it is a page-layout consequence of
  // whatever dockview panel sizes ARE persisted (STORAGE_KEY below), not
  // independent state of its own — recomputing it from cursor history on
  // next load would require replaying a drag that already happened, so
  // instead the box simply resets to its default resting height on reload
  // and grows again on the next drag past the floor, same as before this
  // fix for the very first drag.
  const [growth, setGrowth] = useState(0);
  const growthRef = useRef(0);
  useEffect(() => {
    growthRef.current = growth;
  }, [growth]);
  const dragRef = useRef<{
    startGrowth: number;
    growthOriginY: number | null;
  } | null>(null);

  const reset = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setGrowth(0);
    if (wrapperRef.current) applyWorkspaceHeight(wrapperRef.current, 0);
    const api = apiRef.current;
    if (!api) return;
    [...api.panels].forEach((panel) => api.removePanel(panel));
    buildDefaultLayout(api);
  }, []);
  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) event.api.fromJSON(JSON.parse(saved));
    } catch { localStorage.removeItem(STORAGE_KEY); }
    if (!event.api.panels.length) buildDefaultLayout(event.api);
    event.api.onDidLayoutChange(() => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(event.api.toJSON())); } catch { /* UI-only best effort */ }
    });
  }, []);

  // Native listeners: Dockview creates/destroys sash nodes itself, so
  // there is no React node to attach to. In-box chart-vs-blotter resizes
  // are Dockview's; this only grows the wrapper after the blotter floor.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const bottomRowHeight = () =>
      apiRef.current?.getPanel("positions")?.group?.api.height
      ?? apiRef.current?.getPanel("orders")?.group?.api.height
      ?? Number.POSITIVE_INFINITY;

    const onMouseMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      if (!sashDragShouldGrowWorkspace(bottomRowHeight(), COCKPIT_BOTTOM_ROW_MIN_HEIGHT)) {
        return;
      }
      if (drag.growthOriginY == null) drag.growthOriginY = e.clientY;
      const growthPx = sashDragGrowthPx(e.clientY, drag.growthOriginY);
      growthRef.current = growthPx;
      applyWorkspaceHeight(wrapper, growthPx);
      pinChartHeight(apiRef.current, Math.max(0, wrapper.clientHeight - COCKPIT_BOTTOM_ROW_MIN_HEIGHT));
    };
    const onMouseUp = () => {
      const drag = dragRef.current;
      dragRef.current = null;
      document.removeEventListener("mousemove", onMouseMove);
      const nextGrowth = growthRef.current;
      setGrowth(nextGrowth);
      applyWorkspaceHeight(wrapper, nextGrowth);
      const earned = drag ? nextGrowth - drag.startGrowth : 0;
      if (earned <= 0) return;
      const pinned = Math.max(0, wrapper.clientHeight - COCKPIT_BOTTOM_ROW_MIN_HEIGHT);
      const commit = () => pinChartHeight(apiRef.current, pinned);
      requestAnimationFrame(() => requestAnimationFrame(commit));
    };
    const onMouseDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement | null;
      const sash = target?.closest<HTMLElement>(
        ".dv-split-view-container.dv-vertical > .dv-sash-container > .dv-sash"
      );
      if (!sash) return;
      const atFloor = sashDragShouldGrowWorkspace(bottomRowHeight(), COCKPIT_BOTTOM_ROW_MIN_HEIGHT);
      dragRef.current = {
        startGrowth: growthRef.current,
        growthOriginY: atFloor ? sash.getBoundingClientRect().top : null,
      };
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp, { once: true });
    };

    wrapper.addEventListener("mousedown", onMouseDown);
    return () => {
      wrapper.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
  }, []);

  return (
    // (pb-6 was the original fix for the row butting against the footer
    // with no breathing room; superseded below.)
    // Vertical-space reallocation pass (owner-authorized overshoot,
    // 2026-09-11): pb-6 (24px, "double the pb-3 convention" per the
    // original comment below) was dead space between this box and the
    // footer, with nothing rendered in it. Dropped to pb-2 (8px, real
    // breathing room but not a wasted 24px) — the 16px reclaimed here,
    // plus 16px reclaimed from the footer's own py-4 -> py-2 in App.tsx,
    // is added straight into the box's own height formula below instead
    // of staying an unaccounted-for page margin.
    <div className="px-3 pb-2">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-dim">Trading workspace — move, resize or dock panels</span>
        <button type="button" onClick={reset} className="text-xs text-accent underline">Reset layout</button>
      </div>
      {/* Item 2 of cockpit pass 3 (SUPERSEDED AGAIN, owner override
          2026-09-10, round 2): the resize-y box above (a manual CSS
          `resize: vertical` corner grip) was itself the regression this
          replaces — confirmed live: "difficult to find, tiny corner grip,
          not a proper draggable divider," and its height formula
          (max(560px, 100vh-chrome) + a flat 480px for Positions/Orders)
          ALWAYS exceeded the viewport by construction, forcing the whole
          page to scroll just to reach the workspace, let alone resize it.
          Two changes, together:
          1. The resize-y/overflow-auto/max-h corner-grip hack is gone
             entirely. The chart-vs-Positions/Orders split is now handled
             by dockview's OWN native sibling-panel resize sash (see
             buildDefaultLayout's "positions" panel above,
             direction: "below") — a proper draggable divider with a
             resize cursor and an accent highlight on hover
             (--dv-sash-color/--dv-active-sash-color, styles/index.css),
             not a hand-rolled mechanism. This is strictly more robust
             than continuing to patch the manual approach, and it's what
             the library already does for free between real sibling rows.
          2. The box's total height is viewport-bounded
             (100vh - var(--chrome-h), the same --chrome-h budget
             TopStrip..DecisionStateBanner above already measures live —
             see App.tsx's chromeRef) so the box's own bottom edge lines
             up with the browser window's bottom edge instead of
             guaranteed-overflowing it, with a min-h floor (640px) so a
             day with an unusually tall chrome stack still leaves enough
             room for both a genuinely readable chart and a usable
             Positions/Orders row rather than crushing both to nothing —
             on a short viewport this floor can still require some
             scroll, which is the honest tradeoff for never rendering
             either panel unusably small.
          This still MUST be a definite `height` (not `min-height`) for
          the same reason as before: dockview-react's root renders
          `height: 100%` down its own wrapper chain, which only resolves
          against an ancestor with a definite (not min-) height. The
          `max(...)` CSS function (not Tailwind's arbitrary-value min-h
          alongside a separate h-, which cannot express "whichever of
          these two is bigger" in one definite value) is what makes both
          the floor and the viewport-bound behave as ONE definite
          height.

          Vertical-space reallocation pass (owner-authorized overshoot,
          2026-09-11): at a typical ~1000-1100px viewport the 640px floor
          was the effective governor (100vh - chrome-h regularly landed
          below it), and Positions/Orders needed internal scrolling to
          show 2 rows while real whitespace sat unused below this box and
          above the footer. Two changes together, matching the footer/
          wrapper-padding reclaim above:
          1. Floor raised 640 -> 760 (+120px) — deliberately well past
             "just enough," per the owner's explicit go-further-then-trim
             instruction, rather than another few-pixel nudge.
          2. +32px added to the viewport-bound term — the exact pb-6->
             pb-2 (16px, this file) and footer py-4->py-2 (16px, App.tsx)
             reclaim, folded in here so it isn't left an unaccounted-for
             page margin.
          Both numbers are deliberately generous; this was NOT re-measured
          against a live-rendered page (no browser access from this
          pass) — see the worked pixel math in the accompanying report,
          and dial back the floor first if it overshoots in practice. The
          640px floor's own honest scroll tradeoff (comment above) still
          applies at the new 760px value on short viewports.

          Sash-past-the-floor pass (owner bug report, 2026-09-11, round 2):
          this height was still a hard ceiling — see the
          WORKSPACE_DEFAULT_HEIGHT block comment above for the mechanism
          and the fix. `h-[...]` below keeps providing the resting default
          (still a definite height, still viewport-bound, unchanged from
          the pass above) via Tailwind/CSS as before; the inline `style`
          only ever ADDS to it once `growth` is nonzero, and inline
          `style` always wins the cascade over the class regardless of
          specificity, so there's no fight between the two. */}
      <div
        ref={wrapperRef}
        style={growth > 0 ? { height: `calc(${WORKSPACE_DEFAULT_HEIGHT} + ${growth}px)` } : undefined}
        className="h-[max(760px,calc(100vh-var(--chrome-h)+32px))] rounded-lg border border-border overflow-hidden"
      >
        <DockviewReact className="dockview-theme-qamc" onReady={onReady} components={COMPONENTS} />
      </div>
    </div>
  );
}
