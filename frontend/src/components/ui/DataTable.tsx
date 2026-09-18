import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  useLegacyTable as useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  type LegacyColumnDef,
} from "@tanstack/react-table/legacy";
import { flexRender, type ColumnOrderState, type SortingState } from "@tanstack/react-table";
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from "@tremor/react";
import { readPersistedColumnState, writePersistedColumnState } from "./dataTablePersistence";

/* Column widths are stored as FRACTIONS of the table's own width that
 * always sum to 1, not as absolute pixels.
 *
 * Why (measured live 2026-09-17, owner report "column resizing kind of
 * works"): widths used to come from TanStack's `columnSizing`, whose
 * default per-column size is 150px, rendered as an inline `width: 150px`
 * on every header/body cell. CSS `table-layout: fixed` does NOT clamp a
 * table to its specified `width: 100%` — the spec makes the used width
 * the LARGER of the specified width and the sum of the column widths. In
 * the live cockpit that was 9 columns x 150px = 1350px inside a 743px
 * panel, and the DataTable wrapper's `overflow-x-hidden` (added earlier
 * to kill a sideways scrollbar) silently clipped the overflow. The last
 * four Positions columns — Market value, Day P&L, Unrealized P&L,
 * Sector — and every one of their resize handles were rendered outside
 * the visible box and could not be reached at all, while the first few
 * resized normally. That is the whole of "kind of works".
 *
 * Fractions fix it by construction: the widths are emitted as
 * percentages that sum to 100%, so the table can never be wider than its
 * container and no column can be pushed out of reach. A drag moves ONE
 * boundary — it takes from the column on the right and gives to the
 * column on the left, leaving the total untouched — so the boundary
 * tracks the cursor 1:1 instead of being rescaled by the browser, and no
 * other column jumps. Persisted values are normalized on read, so
 * pixel-valued entries written by the previous build load as sensible
 * ratios rather than needing a migration. */
type ColumnFractions = Record<string, number>;

/** Smallest a column may be dragged, as a share of the table width.
 * Roughly 24px on a 750px panel — narrow enough to park a column you do
 * not care about, wide enough to still find its handle again. */
const MIN_COLUMN_FRACTION = 0.032;

export function normalizeFractions(raw: unknown, ids: string[]): ColumnFractions {
  const source = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const known = ids.filter((id) => {
    const value = source[id];
    return typeof value === "number" && Number.isFinite(value) && value > 0;
  });
  if (!ids.length) return {};
  // Nothing usable persisted (or an entirely new column set): equal split.
  if (!known.length) {
    const share = 1 / ids.length;
    return Object.fromEntries(ids.map((id) => [id, share]));
  }
  // Columns that were added since the widths were saved get the average
  // of the known ones rather than zero, so a new column is never invisible.
  const knownTotal = known.reduce((sum, id) => sum + (source[id] as number), 0);
  const average = knownTotal / known.length;
  const filled = ids.map((id) => [id, known.includes(id) ? (source[id] as number) : average] as const);
  const total = filled.reduce((sum, [, value]) => sum + value, 0);
  return Object.fromEntries(filled.map(([id, value]) => [id, value / total]));
}

// Header/cell padding below is deliberately tighter than Tremor's default
// (p-4 / px-4 py-3.5) - owner override 2026-09-10: Positions' "Day P&L"
// and Orders' "Stop" columns were being pushed off the right edge into
// horizontal scroll by that default padding on wide tables (9-10 columns).
// tremorTwMerge dedupes conflicting Tailwind classes by keeping the one
// passed in last, so this override wins over Tremor's own p-4 without
// touching the Tremor package itself.
// Follow-up same day: px-2.5 (10px/side) still left the row a few px
// wider than the ~580-590px dockview panel, clipping the last column's
// final 1-2 characters and forcing a hairline overflow-x scrollbar. Cut
// to px-2 (8px/side): 2px/side less x2 sides x9-10 columns removes
// ~36-40px from total row width - enough headroom to also survive the
// panel being dragged a bit narrower than today's measured width.
export function DataTable<T extends object>({
  data,
  columns,
  initialSorting = [],
  getRowId,
  onRowClick,
  compact = false,
  resizable = true,
  reorderable = false,
  scrollX = false,
  footer,
  storageKey,
}: {
  data: T[];
  columns: LegacyColumnDef<T, unknown>[];
  initialSorting?: SortingState;
  getRowId?: (row: T) => string;
  onRowClick?: (row: T) => void;
  compact?: boolean;
  /** Adds a drag handle to the right edge of each header cell to
   * resize columns, and persists the resulting widths to localStorage
   * under `storageKey` when one is set. Default on so every blotter
   * can shrink a column to ~1ch (headers truncate) instead of a
   * header-tied min shoving Stop/Target/Limit into horizontal scroll. */
  resizable?: boolean;
  /** Opt-in: adds a drag handle to reorder columns via native HTML5 drag
   * and drop, and persists the resulting order to localStorage under
   * `storageKey`. Default false so existing consumers are unaffected. */
  reorderable?: boolean;
  /** Let the table be WIDER than its container and scroll sideways,
   * instead of squeezing every column into the panel width.
   *
   * Added 2026-09-18 for the Trades blotter (owner request, board item
   * 103). Sixteen columns divided by the panel width is a few characters
   * each — the fractions machinery above deliberately guarantees the
   * table never exceeds its container, which is right for a 9-column
   * blotter and wrong for this one. With `scrollX` the browser's own
   * automatic table layout sizes each column to its content and the
   * horizontal scrollbar carries the remainder; no minimum width is
   * invented here, because the content already implies one. Column
   * resizing is mutually exclusive with it (there is no fixed total to
   * trade width within), so a `scrollX` table is rendered unresizable.
   *
   * The pane around it owns the scrollbars themselves — see
   * DesktopCockpitWorkspace's PANE_SCROLL. */
  scrollX?: boolean;
  /** An extra row rendered below the body, inside the same table, so its
   * cells line up with the columns above (a separate table could not
   * stay aligned once a column is resized or reordered). Keyed by column
   * id; a column with no entry renders an empty cell rather than
   * anything invented. See PositionsPanel's totals row. */
  footer?: Record<string, React.ReactNode>;
  /** Unique per table instance. Required to actually persist when
   * `resizable` or `reorderable` is set (silently skipped without it). */
  storageKey?: string;
}) {
  // Percentage-width columns and horizontal scrolling are mutually
  // exclusive by construction (see `scrollX` above), so one flag governs
  // the whole width machinery below rather than every call site having to
  // remember to pass `resizable={false}` alongside `scrollX`.
  const sizable = resizable && !scrollX;
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const [fractions, setFractions] = useState<ColumnFractions>({});
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [columnOrder, setColumnOrder] = useState<ColumnOrderState>(() =>
    reorderable ? readPersistedColumnState<ColumnOrderState>(storageKey, "order", []) : []
  );
  const [dragColumnId, setDragColumnId] = useState<string | null>(null);
  const stableColumns = useMemo(() => columns, [columns]);

  // Click-vs-drag distinction for "drag from anywhere on the header cell"
  // (2026-09-11): a plain ref, not state, because tracking a pending drag
  // must not itself trigger a render - only crossing the threshold (which
  // calls setDragColumnId below) should. dragTrackingRef is scoped to the
  // single in-flight gesture and is unconditionally nulled out in
  // handleUp below on EVERY release path (drag or click), so a later
  // pointerdown always starts from a clean slate - mirrors the
  // unconditional dragColumnId clear in finishDrag further down.
  const dragTrackingRef = useRef<{
    columnId: string;
    startX: number;
    startY: number;
    dragging: boolean;
  } | null>(null);
  // Set true the instant a tracked pointerdown crosses the movement
  // threshold, consulted (and reset) by the sort button's onClick so a
  // drag that happens to release back over its own origin column doesn't
  // also fire a spurious sort toggle. Reset at the START of every new
  // pointerdown too, so a stale true from a drag that ended off-column
  // (and therefore never reached a click to consume it) can never leak
  // into a later, unrelated click.
  const suppressNextSortClickRef = useRef(false);
  const DRAG_THRESHOLD_PX = 5;

  const beginHeaderPointerTracking = (columnId: string) => (event: React.PointerEvent<HTMLElement>) => {
    if (!reorderable) return;
    if (event.button !== 0) return;
    // Never start reorder-tracking from inside the resize handle's own
    // (deliberately wider, see below) hit area - it has its own
    // mouse/touch-driven resize logic via TanStack's getResizeHandler and
    // must not have its pointerdown swallowed by the drag-anywhere
    // reorder tracking. Bailing out here without preventDefault/
    // stopPropagation lets the native mousedown/touchstart still reach
    // the resize handle's own listeners untouched.
    if (event.target instanceof Element && event.target.closest('[data-resize-handle="true"]')) {
      return;
    }

    suppressNextSortClickRef.current = false;
    const tracking = { columnId, startX: event.clientX, startY: event.clientY, dragging: false };
    dragTrackingRef.current = tracking;

    const handleMove = (moveEvent: PointerEvent) => {
      if (dragTrackingRef.current !== tracking || tracking.dragging) return;
      const dx = moveEvent.clientX - tracking.startX;
      const dy = moveEvent.clientY - tracking.startY;
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
      tracking.dragging = true;
      suppressNextSortClickRef.current = true;
      setDragColumnId(tracking.columnId);
    };

    const cleanup = () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };

    // Fires whether this gesture resolved as a below-threshold click or
    // an actual drag - either way the tracking ref (and this gesture's
    // own listeners) must not survive past release. The reorder drop
    // itself, and clearing dragColumnId, are handled separately by the
    // finishDrag effect below once dragging has been promoted.
    const handleUp = () => {
      if (dragTrackingRef.current === tracking) dragTrackingRef.current = null;
      cleanup();
    };

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
  };

  const table = useReactTable({
    data,
    columns: stableColumns,
    state: {
      sorting,
      columnOrder: reorderable ? columnOrder : undefined,
    },
    onSortingChange: setSorting,
    // Widths are owned by `fractions` above and applied as percentages,
    // not by TanStack's pixel-based columnSizing — see the block comment
    // on ColumnFractions for the measured reason.
    enableColumnResizing: false,
    onColumnOrderChange: reorderable ? setColumnOrder : undefined,
    getRowId,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  // Reconcile a persisted column order against the table's actual leaf
  // columns once on mount: drops ids that no longer exist (a column was
  // removed/renamed) and appends any new ones at the end, so a stale
  // localStorage entry never hides a column that should be visible.
  useEffect(() => {
    if (!reorderable) return;
    const currentIds = table.getAllLeafColumns().map((column) => column.id);
    setColumnOrder((prev) => {
      const known = prev.filter((id) => currentIds.includes(id));
      const missing = currentIds.filter((id) => !known.includes(id));
      const reconciled = [...known, ...missing];
      const unchanged = reconciled.length === prev.length && reconciled.every((id, index) => id === prev[index]);
      return unchanged ? prev : reconciled;
    });
    // Only ever needs to run once, right after mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reorderable]);

  // Visible leaf columns in render order — the only thing the width
  // bookkeeping below cares about. Joined into a string so the effects
  // re-run when a column is added/removed/reordered, not on every render.
  const visibleIds = table.getVisibleLeafColumns().map((column) => column.id);
  const visibleKey = visibleIds.join(",");

  // Seed/reconcile widths against the columns that actually exist.
  // Persisted values are normalized rather than migrated (see the
  // ColumnFractions comment), and a reorder keeps each column's own share.
  useLayoutEffect(() => {
    if (!sizable) return;
    const ids = visibleKey ? visibleKey.split(",") : [];
    setFractions((prev) => {
      // First paint of a table with nothing saved renders with the
      // browser's own automatic table layout (see the `style` on <Table>
      // below), so the header cells are sitting at the width their
      // contents actually want. Seeding from that measurement is what
      // gives a date column more room than a side column without anyone
      // hand-picking numbers — an equal split would be a guess, and the
      // old 150px-per-column default was a guess that also overflowed.
      // useLayoutEffect so the switch happens before the browser paints.
      const measured = Object.fromEntries(
        [...(wrapperRef.current?.querySelectorAll<HTMLElement>("th[data-column-id]") ?? [])]
          .map((cell) => [cell.dataset.columnId ?? "", cell.getBoundingClientRect().width] as const)
          .filter(([id, width]) => id && width > 0),
      );
      const source = Object.keys(prev).length
        ? prev
        : (() => {
            const saved = readPersistedColumnState<ColumnFractions>(storageKey, "sizing", {});
            return Object.keys(saved).length ? saved : measured;
          })();
      const next = normalizeFractions(source, ids);
      const unchanged =
        Object.keys(next).length === Object.keys(prev).length &&
        ids.every((id) => Math.abs((next[id] ?? 0) - (prev[id] ?? 0)) < 1e-6);
      return unchanged ? prev : next;
    });
  }, [sizable, storageKey, visibleKey]);

  useEffect(() => {
    if (!sizable || !Object.keys(fractions).length) return;
    writePersistedColumnState(storageKey, "sizing", fractions);
  }, [sizable, storageKey, fractions]);

  /** Drag one column boundary. All the width moved comes out of the
   * column immediately to the right, so the row's total never changes
   * and nothing can be pushed off the edge. The last column has no
   * boundary of its own — its left-hand neighbour's handle is the one
   * that sizes it — so it gets no handle, which is also why the old
   * handle-clipped-by-the-panel-edge problem cannot come back. */
  const beginColumnResize = (columnId: string) => (event: React.PointerEvent<HTMLElement>) => {
    if (!sizable || event.button !== 0) return;
    const ids = visibleIds;
    const index = ids.indexOf(columnId);
    const nextId = ids[index + 1];
    const width = wrapperRef.current?.clientWidth ?? 0;
    if (!nextId || width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startSelf = fractions[columnId] ?? 1 / ids.length;
    const startNext = fractions[nextId] ?? 1 / ids.length;
    const pair = startSelf + startNext;

    const onMove = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - startX) / width;
      const self = Math.min(
        Math.max(startSelf + delta, MIN_COLUMN_FRACTION),
        Math.max(pair - MIN_COLUMN_FRACTION, MIN_COLUMN_FRACTION),
      );
      setFractions((prev) => ({ ...prev, [columnId]: self, [nextId]: pair - self }));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  useEffect(() => {
    if (!reorderable) return;
    writePersistedColumnState(storageKey, "order", columnOrder);
  }, [reorderable, storageKey, columnOrder]);

  const handleDrop = (targetId: string) => {
    if (!dragColumnId || dragColumnId === targetId) return;
    setColumnOrder((prev) => {
      const base = prev.length ? prev : table.getAllLeafColumns().map((column) => column.id);
      const next = base.filter((id) => id !== dragColumnId);
      const targetIndex = next.indexOf(targetId);
      next.splice(targetIndex, 0, dragColumnId);
      return next;
    });
    // dragColumnId is cleared unconditionally by finishDrag below, not
    // here - see 2026-09-11 fix note on that effect. Clearing it in this
    // function too is redundant on the reorder path and, worse, was the
    // ONLY place it got cleared: a drop back on the origin column (a
    // stray click on the grip, or a drag that snapped back) hit the
    // early return above and skipped it, leaving dragColumnId stuck set
    // and the window pointerup listener attached long after the drag
    // ended - see below.
  };

  // Pointer-driven drag (not native HTML5 DnD - see 2026-09-11 investigation:
  // dragging the grip did nothing live, and native DnD's per-browser gotchas
  // (dataTransfer.setData requirements, drag never initiating inside
  // overflow/scroll containers) made it an unreliable base to keep debugging
  // blind without click-testing access). Mirrors the resize handle's own
  // mouse/touch-driven approach just below, which the owner already
  // confirmed works. Header cells carry `data-column-id` so the column under
  // the pointer at release time can be found via elementFromPoint.
  //
  // 2026-09-11 fix: this used to leave dragColumnId stuck set whenever a
  // drop landed back on its own origin column (handleDrop's early-return
  // path never reset it). While stuck, this effect's dependency on
  // dragColumnId kept it truthy, so the window pointerup/pointercancel
  // listeners below never got torn down - every SUBSEQUENT pointerup
  // anywhere on the page (a sort-button click, a resize-handle release)
  // was replayed through finishDrag/handleDrop as if it were a reorder
  // drop, which is exactly the "clicking headers reorders them" and
  // "resize looks like it undoes itself" behaviour the owner saw. Fix:
  // finishDrag now always clears dragColumnId itself, once, regardless of
  // outcome, so this effect's listeners are only ever live for the
  // duration of one real drag.
  useEffect(() => {
    if (!reorderable || !dragColumnId) return;
    const finishDrag = (event: PointerEvent) => {
      const target = document.elementFromPoint(event.clientX, event.clientY);
      const cell = target instanceof Element ? target.closest<HTMLElement>("[data-column-id]") : null;
      const targetId = cell?.dataset.columnId;
      if (targetId) handleDrop(targetId);
      setDragColumnId(null);
    };
    const cancelDrag = () => setDragColumnId(null);
    window.addEventListener("pointerup", finishDrag);
    window.addEventListener("pointercancel", cancelDrag);
    return () => {
      window.removeEventListener("pointerup", finishDrag);
      window.removeEventListener("pointercancel", cancelDrag);
    };
    // handleDrop closes over dragColumnId/table each render; re-subscribing
    // whenever dragColumnId changes keeps the listener's closure current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reorderable, dragColumnId]);

  return (
    <div
      ref={wrapperRef}
      // `overflow-x-hidden` is right for a table pinned to its container
      // width (the default) and wrong for a `scrollX` one, where clipping
      // is exactly what put half the Trades columns out of reach.
      // `table-scroll-x` re-enables Tremor's own inner scroll region and
      // releases its `w-full` table — both are needed, and neither is
      // reachable from a Tailwind class on this element. See
      // styles/index.css.
      className={`max-w-full min-w-0 rounded-lg ring-1 ring-border ${scrollX ? "table-scroll-x overflow-x-auto" : "overflow-x-hidden"}`}
    >
      {/* `table-layout: fixed` only once the widths are known. Until
          then the browser's automatic layout runs, which is what the
          seeding effect above measures. */}
      <Table
        className={`${compact ? "text-xs" : "text-sm"} ${scrollX ? "w-auto" : "overflow-x-hidden"}`}
        style={sizable ? { width: "100%", tableLayout: Object.keys(fractions).length ? "fixed" : "auto" } : undefined}
      >
        <TableHead>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHeaderCell
                  key={header.id}
                  data-column-id={header.column.id}
                  // `overflow-hidden` deliberately does NOT go on this cell
                  // (measured 2026-09-17): it clipped the absolutely
                  // positioned resize handle below to the cell box, halving
                  // its 14px hit area to the 7px that happened to fall
                  // inside — one real cause of "the drag target is hard to
                  // hit". Truncation now happens on the inner content
                  // wrapper, which looks identical and leaves the handle
                  // whole.
                  className={`relative px-2 py-2 ${sizable ? "" : "whitespace-nowrap"} ${reorderable ? "cursor-grab select-none" : ""}`}
                  style={sizable ? { width: `${(fractions[header.column.id] ?? 0) * 100}%` } : undefined}
                  // Drag-anywhere reorder (2026-09-11): pointerdown on ANY
                  // part of the header cell starts click-vs-drag tracking,
                  // not just the small grip icon below. The grip stays as
                  // a visual affordance only - it no longer needs its own
                  // pointer handling since it's just a normal part of the
                  // cell now.
                  onPointerDown={reorderable ? beginHeaderPointerTracking(header.column.id) : undefined}
                >
                  <div className={`flex min-w-0 items-center gap-1.5 ${sizable ? "overflow-hidden" : ""}`}>
                    {reorderable && (
                      <span
                        className={`pointer-events-none select-none text-border ${
                          dragColumnId === header.column.id ? "text-accent" : ""
                        }`}
                        title="Drag anywhere on this header to reorder"
                      >
                        ⠿
                      </span>
                    )}
                    {header.isPlaceholder ? null : header.column.getCanSort() ? (
                      <button
                        type="button"
                        onClick={(event) => {
                          // A drag that crossed the movement threshold and
                          // happened to release back over its own column
                          // still delivers a native click here (mousedown
                          // and mouseup landed on the same element) - this
                          // guard is what stops that from also toggling
                          // sort. Below-threshold releases (real clicks)
                          // leave the flag false and fall through to the
                          // normal sort-toggle handler untouched.
                          if (suppressNextSortClickRef.current) {
                            suppressNextSortClickRef.current = false;
                            event.preventDefault();
                            return;
                          }
                          header.column.getToggleSortingHandler()?.(event);
                        }}
                        className="min-w-0 flex-1 truncate text-left uppercase tracking-wide"
                        title={typeof header.column.columnDef.header === "string" ? header.column.columnDef.header : undefined}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {{ asc: " ↑", desc: " ↓" }[header.column.getIsSorted() as string] ?? ""}
                      </button>
                    ) : (
                      <span className="min-w-0 truncate" title={typeof header.column.columnDef.header === "string" ? header.column.columnDef.header : undefined}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                      </span>
                    )}
                  </div>
                  {sizable && visibleIds.indexOf(header.column.id) < visibleIds.length - 1 && (
                    // 14px INTERACTIVE hit area centred on the column
                    // boundary (right: -7px + w-3.5), now genuinely 14px
                    // wide since the cell no longer clips it. The VISUAL
                    // strip inside stays the original 6px flush against the
                    // boundary, so nothing moves on screen — only the
                    // grabbable area is real. `group` so hovering anywhere
                    // in the hit area lights the strip.
                    //
                    // No handle on the LAST column: its right edge is the
                    // table's own edge, there is nothing on the far side to
                    // trade width with, and a handle there used to hang
                    // outside the wrapper's overflow-x-hidden where it could
                    // not be grabbed. Its width is set from its left
                    // neighbour's handle instead.
                    <div
                      data-resize-handle="true"
                      onPointerDown={beginColumnResize(header.column.id)}
                      className="group absolute top-0 z-10 h-full w-3.5 cursor-col-resize touch-none select-none"
                      style={{ right: "-7px" }}
                      title="Drag to resize this column"
                    >
                      <div className="absolute right-[7px] top-0 h-full w-1.5 group-hover:bg-border" />
                    </div>
                  )}
                </TableHeaderCell>
              ))}
            </TableRow>
          ))}
        </TableHead>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow
              key={row.id}
              className={onRowClick ? "cursor-pointer hover:bg-panel-alt" : ""}
              tabIndex={onRowClick ? 0 : undefined}
              onClick={() => onRowClick?.(row.original)}
              onKeyDown={(event) => {
                if (!onRowClick || (event.key !== "Enter" && event.key !== " ")) return;
                event.preventDefault();
                onRowClick(row.original);
              }}
            >
              {row.getVisibleCells().map((cell) => (
                <TableCell
                  key={cell.id}
                  className={`font-mono tabular-nums px-2 py-2 ${sizable ? "overflow-hidden truncate" : "whitespace-nowrap"}`}
                  style={sizable ? { width: `${(fractions[cell.column.id] ?? 0) * 100}%` } : undefined}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
          {footer && (
            // Deliberately loud: the owner asked for a totals row that
            // stands out, so it gets a heavier top border, a tinted
            // background, bold text and a size up from the data rows.
            // `tabular-nums` (as on every data cell above) keeps each
            // digit the same width, so the total's digits line up
            // vertically with the column they total.
            <TableRow className="border-t-2 border-accent/70 bg-panel-alt font-bold">
              {table.getVisibleLeafColumns().map((column) => (
                <TableCell
                  key={`total-${column.id}`}
                  className={`font-mono tabular-nums px-2 py-3 text-[0.9rem] ${sizable ? "overflow-hidden truncate" : "whitespace-nowrap"}`}
                  style={sizable ? { width: `${(fractions[column.id] ?? 0) * 100}%` } : undefined}
                >
                  {/* No entry for this column means an EMPTY cell. A
                      column whose sum would be meaningless must show
                      nothing rather than a number invented to fill it. */}
                  {footer[column.id] ?? null}
                </TableCell>
              ))}
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
