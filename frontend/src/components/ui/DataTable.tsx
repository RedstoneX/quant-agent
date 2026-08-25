import { useMemo, useState } from "react";
import {
  useLegacyTable as useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  type LegacyColumnDef,
} from "@tanstack/react-table/legacy";
import { flexRender, type SortingState } from "@tanstack/react-table";
import { Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow } from "@tremor/react";

export function DataTable<T extends object>({
  data,
  columns,
  initialSorting = [],
  getRowId,
  onRowClick,
  compact = false,
}: {
  data: T[];
  columns: LegacyColumnDef<T, unknown>[];
  initialSorting?: SortingState;
  getRowId?: (row: T) => string;
  onRowClick?: (row: T) => void;
  compact?: boolean;
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const stableColumns = useMemo(() => columns, [columns]);
  const table = useReactTable({
    data,
    columns: stableColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getRowId,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="max-w-full overflow-x-auto rounded-lg ring-1 ring-border">
      <Table className={compact ? "text-xs" : "text-sm"}>
        <TableHead>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHeaderCell key={header.id} className="whitespace-nowrap">
                  {header.isPlaceholder ? null : header.column.getCanSort() ? (
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      className="w-full text-left uppercase tracking-wide"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {{ asc: " ↑", desc: " ↓" }[header.column.getIsSorted() as string] ?? ""}
                    </button>
                  ) : (
                    flexRender(header.column.columnDef.header, header.getContext())
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
                <TableCell key={cell.id} className="whitespace-nowrap font-mono tabular-nums">
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
