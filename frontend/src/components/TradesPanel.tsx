import { useMemo, useState } from "react";
import { Badge, TextInput } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { TradeItem } from "../api/client";
import { fmtMoney, fmtTime, pnlClass } from "../lib/format";
import { displayFillPrice, displayFillQty } from "../lib/tradeFillDisplay";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";

const columnHelper = createColumnHelper<TradeItem>();

export function TradeTable({ trades, onInspect }: { trades: TradeItem[]; onInspect?: (trade: TradeItem) => void }) {
  const columns = useMemo(
    () => [
      columnHelper.accessor("timestamp", { header: "Time", cell: (info) => fmtTime(info.getValue()) }),
      columnHelper.accessor("symbol", { header: "Symbol", cell: (info) => <span className="font-bold text-accent">{info.getValue()}</span> }),
      columnHelper.accessor("action", { header: "Action", cell: (info) => <Badge color={info.getValue().includes("BUY") ? "emerald" : info.getValue().includes("SELL") ? "rose" : "slate"} size="xs">{info.getValue()}</Badge> }),
      columnHelper.accessor("fill_status", { header: "Fill status", cell: (info) => <Badge color={String(info.getValue()).includes("fill") ? "emerald" : String(info.getValue()).includes("reject") ? "rose" : "slate"} size="xs">{info.getValue() || "unfilled"}</Badge> }),
      columnHelper.accessor("fill_qty", { header: "Filled qty", cell: (info) => displayFillQty(info.row.original) }),
      columnHelper.accessor("fill_price", { header: "Fill price", cell: (info) => displayFillPrice(info.row.original) }),
      columnHelper.accessor("realized_pnl", { header: "Realized P&L", cell: (info) => <span className={pnlClass(info.getValue())}>{info.getValue() === null || info.getValue() === undefined ? "—" : fmtMoney(info.getValue())}</span> }),
      columnHelper.accessor("stop_loss", { header: "Recorded stop", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("take_profit", { header: "Take profit", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("run_id", { header: "Run", cell: (info) => info.getValue() || "—" }),
      columnHelper.accessor("decision_id", { header: "Decision", cell: (info) => info.getValue() || "—" }),
    ] as LegacyColumnDef<TradeItem, unknown>[],
    []
  );
  return <DataTable data={trades} columns={columns} getRowId={(trade) => String(trade.id)} initialSorting={[{ id: "timestamp", desc: true }]} onRowClick={onInspect} />;
}

export function TradesPanel({
  trades,
  error,
  loading,
  onInspect,
}: {
  trades: TradeItem[];
  error: string | null;
  loading: boolean;
  onInspect?: (trade: TradeItem) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toUpperCase();
    return needle
      ? trades.filter((trade) => trade.symbol.includes(needle) || (trade.run_id || "").toUpperCase().includes(needle))
      : trades;
  }, [trades, query]);
  const status = error ? "degraded" : loading ? "loading" : "ok";
  return (
    <Panel
      title="Recent trades"
      subtitle="Recorded stop is the execution record, not proof of a live broker stop. Select a linked trade for protection evidence."
      status={status}
      actions={<TextInput value={query} onValueChange={setQuery} placeholder="Symbol or run" className="w-48 !bg-panel-alt !ring-border" />}
    >
      {error && <StateMessage text={`Could not load trades: ${error}`} error />}
      {!error && trades.length === 0 && <StateMessage text="No trades recorded yet." />}
      {!error && trades.length > 0 && filtered.length === 0 && <StateMessage text="No trades match this filter." />}
      {!error && filtered.length > 0 && (
        <TradeTable trades={filtered} onInspect={onInspect} />
      )}
    </Panel>
  );
}
