import { useMemo, useState } from "react";
import { Badge, Select, SelectItem, TextInput } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { OrderItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";

const columnHelper = createColumnHelper<OrderItem>();

export function OrdersPanel({
  orders,
  error,
  loading,
  status: orderStatus,
  onStatusChange,
  onInspect,
  onSelectSymbol,
}: {
  orders: OrderItem[];
  error: string | null;
  loading: boolean;
  status: string;
  onStatusChange: (status: "open" | "closed" | "all") => void;
  onInspect?: (order: OrderItem) => void;
  /** Symbol-cell-specific click: charts the symbol in place, same as
   * PositionsPanel's onSelectSymbol. Deliberately separate from onInspect
   * above (which opens the order's Run/Candidate Detail modal on the rest
   * of the row) — clicking just the SYMBOL must never open a modal, it
   * must behave exactly like clicking a symbol in Positions. See
   * DataTable's row-level onClick, which this cell's own stopPropagation
   * has to defeat. */
  onSelectSymbol?: (symbol: string) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toUpperCase();
    return needle ? orders.filter((order) => order.symbol.includes(needle) || order.id.toUpperCase().includes(needle)) : orders;
  }, [orders, query]);
  const columns = useMemo(
    () => [
      columnHelper.accessor("symbol", {
        header: "Symbol",
        cell: (info) =>
          onSelectSymbol ? (
            <button
              type="button"
              className="font-bold text-accent hover:underline"
              onClick={(event) => {
                event.stopPropagation();
                onSelectSymbol(info.getValue());
              }}
            >
              {info.getValue()}
            </button>
          ) : (
            <span className="font-bold text-accent">{info.getValue()}</span>
          ),
      }),
      columnHelper.accessor("side", { header: "Side", cell: (info) => (info.getValue() || "—").toUpperCase() }),
      columnHelper.accessor("order_type", { header: "Type", cell: (info) => (info.getValue() || "—").replace(/_/g, " ") }),
      columnHelper.accessor("qty", { header: "Requested", cell: (info) => fmtNum(info.getValue()) }),
      columnHelper.accessor("status", {
        header: "Status",
        cell: (info) => <Badge color={String(info.getValue()).includes("fill") ? "emerald" : String(info.getValue()).includes("reject") ? "rose" : "slate"} size="xs">{info.getValue() || "unknown"}</Badge>,
      }),
      columnHelper.accessor("filled_qty", {
        header: "Fill",
        cell: (info) => `${fmtNum(info.getValue())} @ ${fmtMoney(info.row.original.filled_avg_price)}`,
      }),
      columnHelper.accessor("limit_price", { header: "Limit", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("stop_price", { header: "Stop", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("submitted_at", { header: "Submitted", cell: (info) => fmtTime(info.getValue()) }),
      columnHelper.accessor("filled_at", { header: "Filled", cell: (info) => fmtTime(info.getValue()) }),
    ] as LegacyColumnDef<OrderItem, unknown>[],
    [onSelectSymbol]
  );
  const panelStatus = error ? "degraded" : loading ? "loading" : "ok";

  return (
    <Panel
      title="Orders"
      subtitle="Broker order facts. Select a linked order to inspect its chart and lifecycle."
      status={panelStatus}
      actions={
        <div className="flex min-w-[260px] gap-2">
          <TextInput value={query} onValueChange={setQuery} placeholder="Symbol or order ID" className="!bg-panel-alt !ring-border" />
          <Select value={orderStatus} onValueChange={(value) => onStatusChange(value as "open" | "closed" | "all")} className="w-28 !bg-panel-alt">
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="closed">Closed</SelectItem>
            <SelectItem value="all">All</SelectItem>
          </Select>
        </div>
      }
    >
      {error && <StateMessage text={`Orders read failed: ${error}`} error />}
      {!error && orders.length === 0 && <StateMessage text={`No ${orderStatus} orders.`} />}
      {!error && orders.length > 0 && filtered.length === 0 && <StateMessage text="No orders match this filter." />}
      {!error && filtered.length > 0 && (
        <DataTable
          data={filtered}
          columns={columns}
          getRowId={(order) => order.id}
          initialSorting={[{ id: "submitted_at", desc: true }]}
          onRowClick={onInspect}
        />
      )}
    </Panel>
  );
}

export function useOrderStatus() {
  return useState<"open" | "closed" | "all">("open");
}
