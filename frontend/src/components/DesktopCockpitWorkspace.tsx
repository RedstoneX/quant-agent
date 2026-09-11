import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card, Text } from "@tremor/react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { useCockpitWorkspace } from "../context/CockpitWorkspaceContext";
import { useSupportWorkspace } from "../context/SupportWorkspaceContext";
import { useModalActions } from "../context/ModalContext";
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
    <div className="flex h-full min-w-0 flex-col overflow-hidden p-2 gap-2">
      <Card className="flex flex-shrink-0 !bg-panel-alt !p-2.5 !ring-border">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
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
      </Card>
      {heldPosition && (
        <div className="flex-shrink-0">
          <PositionHoldingStrip position={heldPosition} openOrders={support.openOrders} trades={support.trades} />
        </div>
      )}
      <div className="flex-shrink-0">
        <DecisionSummaryLine funnel={state.funnel} symbol={state.chartSymbol} />
      </div>
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
const STORAGE_KEY = "qamc.dockview.cockpit.v6";

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
  // Owner override 2026-09-10 ("let's make the chart bigger, which means
  // I sacrifice the positions and orders panel"): initialHeight dropped
  // from 480 to 230 — a real but deliberately modest default (enough for
  // a table header plus ~2-3 data rows; see PositionsPanel/DataTable's
  // own row height, nothing new introduced here) so the chart row above
  // gets the majority of whatever height the workspace box actually has
  // by default. This is a STARTING size only, per dockview's own resize
  // sash above — the operator can drag it taller at any time, and that
  // choice persists via the existing onDidLayoutChange localStorage save
  // below, same as every other panel arrangement in this workspace.
  //
  // Fix (owner bug report, 2026-09-10, round 3): the sash above had NO
  // floor at all — dragging the chart bigger could crush this row to
  // ~90px, just the Panel title bar with a single data row requiring the
  // panel's own internal scrollbar to see a second one. minimumHeight is
  // dockview's own native per-panel constraint (Constraints in
  // gridview/gridviewPanel.d.ts, accepted directly by api.addPanel — see
  // AddPanelOptions & Partial<Constraints> in dockview/options.d.ts), so
  // the sash itself simply stops/clamps at the floor rather than fighting
  // a separate CSS min-height against dockview's own layout math.
  // Calculated, not guessed, from DataTable.tsx's own Tailwind classes:
  // each row (header or data) is `px-2 py-2` (16px padding) on `text-sm`
  // (20px line-height) = 36px/row, exact. Non-table chrome stacked above
  // that inside this same row: dockview's own tab strip
  // (--dv-tabs-and-actions-container-height: 34px, styles/index.css,
  // exact) + Panel.tsx's `.panel-head` title/subtitle bar (~57px,
  // estimated from its own py-2.5 padding + two wrapped text lines, not
  // measured live) + `.panel-body`'s `py-3` padding (24px, exact) = ~115px
  // fixed overhead. 115 + 36 * 4 (header + 3 full data rows) = 259,
  // rounded to 260 — enough that the row can never be dragged below
  // showing the table header plus a genuine 3 data rows, which is what
  // was actually missing (today's 230 default lands closer to header +
  // 2 rows once this same chrome is accounted for, which is why the bug
  // was reachable at all). This does mean the resting default nudges up
  // slightly from 230 to 260 on any layout that was sitting below the new
  // floor — the tradeoff for a floor that's actually enough to read a
  // second/third position without scrolling.
  // Owner override 2026-09-11 (vertical-space reallocation pass): the
  // owner reversed the 2026-09-10 "sacrifice positions/orders for a
  // bigger chart" trade — that 230 default sat BELOW the 260
  // minimumHeight floor computed above (dockview clamps it up to the
  // floor on layout, so 230 never actually rendered), and the floor
  // itself only fit header+3 rows with no room for the 20px bottom
  // padding added in an earlier fix that same day. Raised to 340 — floor
  // math (115px fixed chrome + 36px/row) puts that at header + 5 full
  // rows + the 20px padding with room to spare, so the panel starts
  // comfortably past "3+ rows" rather than exactly at it. This is a
  // STARTING size only (see minimumHeight/dockview sash comments below);
  // the operator can still drag it to any size, which persists as
  // before.
  api.addPanel({
    id: "positions",
    component: "positions",
    title: "Positions",
    position: { referencePanel: "chart", direction: "below" },
    initialHeight: 340,
    minimumHeight: 260,
  });
  api.addPanel({ id: "candidates", component: "candidates", title: "Candidates", position: { referencePanel: "positions", direction: "within" }, inactive: true });
  api.addPanel({ id: "orders", component: "orders", title: "Orders", position: { referencePanel: "positions", direction: "right" }, minimumHeight: 260 });
  api.addPanel({ id: "trades", component: "trades", title: "Trades", position: { referencePanel: "orders", direction: "within" }, inactive: true });

  api.getPanel("positions")?.api.setActive();
}

// Owner bug report, 2026-09-11 ("dragging the chart bigger just stops
// working"): the workspace box below used to have a DEFINITE, fixed
// total height (the calc() in WORKSPACE_DEFAULT_HEIGHT). Dockview-react
// fills whatever box it's given — it watches its own container with an
// internal ResizeObserver and re-lays-out to fill it (dockview-core's
// `disableAutoResizing` option, default off); it has no mechanism to grow
// that container itself, only to redistribute the space already inside
// it. So once the Positions/Orders row hit its minimumHeight floor (see
// buildDefaultLayout above), the chart/positions sash had nowhere left to
// take space FROM — the drag kept firing mouse events but dockview
// silently had nothing to give, so it looked like the drag "stopped
// working" past that point.
//
// First fix attempt (didn't work, root cause found 2026-09-11 round 3):
// watched dockview's sash for `.dv-minimum` and grew the box while that
// class was present. Traced live against dockview-core's actual
// `updateSashEnablement()` (splitview implementation): for a two-view
// vertical split, the class applied when the LOWER view (Positions/
// Orders) is pinned at its own minimumSize and the upper view (chart)
// still has room is `.dv-maximum`, not `.dv-minimum` — `.dv-minimum` is
// the class for the opposite edge (chart pinned at ITS floor). The
// listener was watching a class that is never set in this drag
// direction, so `atFolor` was permanently false and growth never fired.
// That's a real dockview-core behavior (verified by reading
// `updateSashEnablement` in dockview-core's built output, not assumed),
// but relying on it also means silently re-deriving dockview's internal
// multi-view collapse/expand algorithm for every layout shape a user can
// rearrange into — fragile even with the correct class name.
//
// Fix (this pass): drop class-watching entirely. Track the mouse
// directly during a sash drag and compute the needed growth purely from
// cursor position — see the mousedown/mousemove handlers below. This
// sidesteps dockview's internal clamp state altogether: growth is a pure
// function of "how far past the natural (ungrown) floor-line has the
// mouse gone," independent of whatever class dockview-core happens to
// apply this version or that layout shape.
//
// The box rests at exactly WORKSPACE_DEFAULT_HEIGHT. Positions/Orders'
// own minimumHeight (260, set in buildDefaultLayout) still does the
// actual floor-holding inside dockview; this code only ever ADDS height
// on top of the resting default, never removes any, so it cannot itself
// crush anything below that floor. The visible effect: past the old
// ceiling, the box keeps growing 1:1 with the drag instead of clamping,
// which pushes the footer down and requires page scroll — the explicit
// trade the owner asked for.
//
// Round-4 fix (owner bug report, 2026-09-11, "the direct sash stopped
// responding"): the trigger line below used to be computed as
// wrapper-bottom minus a single hard-coded 260px constant (one
// Positions/Orders row's floor). That only holds for the DEFAULT
// side-by-side layout — the moment Positions/Orders end up STACKED (two
// floors, ~520px reserved, not 260px), the line sat ~260px below the
// sash's real, already-maxed position, so a drag starting right at the
// visible sash needed ~260px of dead motion before growth ever kicked
// in — reading exactly as "the sash doesn't respond," while a drag that
// wandered far enough down (past Positions, into/under Orders) crossed
// the erroneous line anyway and did grow the chart. Fixed in the
// mousedown handler below by reading the sash's own live position
// instead of assuming a fixed reserved-floor constant.
const WORKSPACE_DEFAULT_HEIGHT = "max(760px, calc(100vh - var(--chrome-h) + 32px))";

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
  // Mirrors `growth` for the native mousedown handler below (which closes
  // over refs, not state, since it's registered once and dockview's sash
  // nodes come and go outside React's render cycle).
  const growthRef = useRef(0);
  useEffect(() => {
    growthRef.current = growth;
  }, [growth]);
  // Tracks the in-progress sash drag, if any: the Y coordinate (viewport
  // px) beyond which the mouse has gone past what the box's NATURAL
  // (ungrown) height can give the sash — i.e. wrapper's bottom edge with
  // today's growth backed out, minus the fixed floor reserved for
  // Positions/Orders. Computed once at mousedown; see onMouseDown below.
  // startGrowth is this same drag's `growth` value AT mousedown, so
  // mouseup can work out exactly how much THIS drag added (see the
  // dockview-commit fix in onMouseUp below).
  const dragRef = useRef<{ naturalMaxY: number; startGrowth: number } | null>(null);

  const reset = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setGrowth(0);
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

  // See the WORKSPACE_DEFAULT_HEIGHT comment above for why this exists,
  // and for why this is geometry-driven rather than watching dockview's
  // own sash state. Native DOM listeners (not React synthetic ones)
  // because dockview's sash element is created and destroyed by
  // dockview-core itself, deep inside DockviewReact's own tree — there is
  // no React node of ours to attach a handler to directly, so this
  // delegates from the wrapper.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const onMouseMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      // Pure function of cursor position: how far below the natural
      // (ungrown) floor-line has the mouse gone. Growth is recomputed in
      // full on every move rather than accumulated, so dragging back up
      // shrinks it smoothly and dragging down keeps extending it with no
      // ceiling — no dependency on dockview's internal clamp/class state.
      const growthPx = Math.max(0, e.clientY - drag.naturalMaxY);
      // Written synchronously here (not left to the [growth]-effect above,
      // which only fires after React commits + a passive-effect pass) so
      // onMouseUp below can read the exact just-set value the instant the
      // drag ends, with no risk of reading a render-cycle-stale ref.
      growthRef.current = growthPx;
      setGrowth(growthPx);
    };
    // Root-cause (traced against dockview-core's actual built output, not
    // assumed): this sash is a REAL dockview-core sash — dockview attaches
    // its OWN native `pointerdown` drag handler directly on it
    // (splitview.ts's `addView`), which runs independently of the growth
    // tracking above and is not disabled by it. That native handler saves
    // dockview's internal chart/positions size ratio (`saveProportions()`)
    // on ITS OWN pointerup, using whatever sizes dockview had at that exact
    // instant — which is the PRE-growth split, because dockview only learns
    // the wrapper actually got taller asynchronously, via a ResizeObserver
    // reacting to the CSS `growth` change above (React state -> re-render ->
    // paint -> observer callback, several ticks later than the drag itself).
    // The next time that observer fires — which reliably happens right
    // around mouseup, once the browser has painted the final grown height —
    // dockview redistributes the (now larger) total using those STALE,
    // pre-growth proportions, handing the chart back roughly its ORIGINAL
    // share and giving the rest to Positions/Orders. The wrapper's own CSS
    // height (`growth` state, set above) never actually resets — nothing
    // clears it outside the "Reset layout" button — but the chart panel
    // inside visibly shrinks back right as the mouse comes up, which reads
    // exactly like "the whole thing snaps back."
    //
    // Fix: once a drag that earned real growth ends, commit that growth
    // straight into dockview's OWN layout via its public group API
    // (`setSize`) instead of leaving dockview to find out about it later
    // through the racy ResizeObserver path. This makes dockview save FRESH
    // proportions that already include the growth, so the very next
    // relayout (quote poll, any re-render, the async observer catching up)
    // preserves it instead of overwriting it.
    const onMouseUp = () => {
      const drag = dragRef.current;
      dragRef.current = null;
      document.removeEventListener("mousemove", onMouseMove);
      const earned = drag ? growthRef.current - drag.startGrowth : 0;
      if (earned > 0) {
        const chartGroup = apiRef.current?.getPanel("chart")?.group;
        if (chartGroup) {
          const currentHeight = chartGroup.api.height ?? 0;
          chartGroup.api.setSize({ height: currentHeight + earned });
        }
      }
    };
    const onMouseDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement | null;
      // Scoped to a vertical-split sash (stacked top/bottom, i.e. the
      // chart-vs-Positions/Orders divider and any other row split a user
      // creates by rearranging panels) — dockview.css's own selector shape
      // for that sash kind, not a guess at a class name. Only used to
      // identify that a relevant drag started; the growth math below never
      // reads anything else off this element or off dockview's state.
      const sash = target?.closest<HTMLElement>(
        ".dv-split-view-container.dv-vertical > .dv-sash-container > .dv-sash"
      );
      if (!sash) return;
      // Round-4 fix — see the block comment above WORKSPACE_DEFAULT_HEIGHT
      // for the full trace. The trigger line is the sash's OWN live
      // position, not a hard-coded reserved-floor constant: dockview has
      // already placed this sash at exactly the true natural-maximum
      // boundary for whatever the CURRENT panel arrangement is (one row,
      // two stacked rows, or anything else the user rearranges it into),
      // so reading it directly is correct for every topology with no
      // assumption baked in.
      const naturalMaxY = sash.getBoundingClientRect().top;
      dragRef.current = { naturalMaxY, startGrowth: growthRef.current };
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
