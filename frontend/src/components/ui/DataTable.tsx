import { useEffect, useMemo, useRef, useState } from "react";
import {
  useLegacyTable as useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  type LegacyColumnDef,
} from "@tanstack/react-table/legacy";
import { flexRender, type ColumnOrderState, type ColumnSizingState, type SortingState } from "@tanstack/react-table";
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from "@tremor/react";
import { readPersistedColumnState, writePersistedColumnState } from "./dataTablePersistence";

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
  resizable = false,
  reorderable = false,
  storageKey,
}: {
  data: T[];
  columns: LegacyColumnDef<T, unknown>[];
  initialSorting?: SortingState;
  getRowId?: (row: T) => string;
  onRowClick?: (row: T) => void;
  compact?: boolean;
  /** Opt-in: adds a drag handle to the right edge of each header cell to
   * resize columns, and persists the resulting widths to localStorage
   * under `storageKey`. Default false so existing consumers are unaffected. */
  resizable?: boolean;
  /** Opt-in: adds a drag handle to reorder columns via native HTML5 drag
   * and drop, and persists the resulting order to localStorage under
   * `storageKey`. Default false so existing consumers are unaffected. */
  reorderable?: boolean;
  /** Unique per table instance. Required to actually persist when
   * `resizable` or `reorderable` is set (silently skipped without it). */
  storageKey?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const [columnSizing, setColumnSizing] = useState<ColumnSizingState>(() =>
    resizable ? readPersistedColumnState<ColumnSizingState>(storageKey, "sizing", {}) : {}
  );
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
      columnSizing: resizable ? columnSizing : undefined,
      columnOrder: reorderable ? columnOrder : undefined,
    },
    onSortingChange: setSorting,
    onColumnSizingChange: resizable ? setColumnSizing : undefined,
    enableColumnResizing: resizable,
    columnResizeMode: "onChange",
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

  useEffect(() => {
    if (!resizable) return;
    writePersistedColumnState(storageKey, "sizing", columnSizing);
  }, [resizable, storageKey, columnSizing]);

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
    <div className="max-w-full overflow-x-auto rounded-lg ring-1 ring-border">
      <Table className={compact ? "text-xs" : "text-sm"} style={resizable ? { width: table.getTotalSize() } : undefined}>
        <TableHead>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHeaderCell
                  key={header.id}
                  data-column-id={header.column.id}
                  className={`relative whitespace-nowrap px-2 py-2 ${reorderable ? "cursor-grab select-none" : ""}`}
                  style={resizable ? { width: header.getSize() } : undefined}
                  // Drag-anywhere reorder (2026-09-11): pointerdown on ANY
                  // part of the header cell starts click-vs-drag tracking,
                  // not just the small grip icon below. The grip stays as
                  // a visual affordance only - it no longer needs its own
                  // pointer handling since it's just a normal part of the
                  // cell now.
                  onPointerDown={reorderable ? beginHeaderPointerTracking(header.column.id) : undefined}
                >
                  <div className="flex items-center gap-1.5">
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
                        className="flex-1 text-left uppercase tracking-wide"
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {{ asc: " ↑", desc: " ↓" }[header.column.getIsSorted() as string] ?? ""}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </div>
                  {resizable && header.column.getCanResize() && (
                    // Widened (14px) INTERACTIVE hit area, centered on the
                    // actual column boundary (right: -7px + w-3.5/14px, so
                    // it spans 7px either side of the cell's right edge -
                    // the same edge the old flush-right 6px strip sat
                    // against). The VISUAL strip inside stays the original
                    // 6px/right-aligned-to-boundary size and position, so
                    // the rendered border doesn't shift - only the
                    // grabbable area around it grows. `group` so hovering
                    // anywhere in the wider hit area still lights up the
                    // thin visual strip, not just the strip itself.
                    <div
                      data-resize-handle="true"
                      onMouseDown={header.getResizeHandler()}
                      onTouchStart={header.getResizeHandler()}
                      className="group absolute top-0 z-10 h-full w-3.5 cursor-col-resize touch-none select-none"
                      style={{ right: "-7px" }}
                    >
                      <div
                        className={`absolute right-[7px] top-0 h-full w-1.5 ${
                          header.column.getIsResizing() ? "bg-accent" : "group-hover:bg-border"
                        }`}
                      />
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
                  className="whitespace-nowrap font-mono tabular-nums px-2 py-2"
                  style={resizable ? { width: cell.column.getSize() } : undefined}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
