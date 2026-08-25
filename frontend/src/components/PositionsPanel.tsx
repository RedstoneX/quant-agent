import { useMemo } from "react";
import { Badge, Callout } from "@tremor/react";
import { legacyCreateColumnHelper as createColumnHelper, type LegacyColumnDef } from "@tanstack/react-table/legacy";
import { PositionItem } from "../api/client";
import { fmtMoney, fmtNum, pnlClass } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { DataTable } from "./ui/DataTable";

const columnHelper = createColumnHelper<PositionItem>();

export function PositionsPanel({
  positions,
  error,
  loading,
  updatedAt,
  onSelectSymbol,
}: {
  positions: PositionItem[];
  error: string | null;
  loading: boolean;
  updatedAt?: Date | null;
  onSelectSymbol?: (symbol: string) => void;
}) {
  const everLoaded = Boolean(updatedAt);
  const status = error ? (everLoaded ? "stale" : "error") : loading ? "loading" : "ok";
  const columns = useMemo(
    () => [
      columnHelper.accessor("symbol", {
        header: "Symbol",
        cell: (info) => (
          <button type="button" className="font-bold text-accent hover:underline" onClick={() => onSelectSymbol?.(info.getValue())}>
            {info.getValue()}
          </button>
        ),
      }),
      columnHelper.accessor("direction", {
        header: "Role",
        cell: (info) => (
          <Badge color={info.row.original.is_cash_equivalent ? "slate" : info.getValue() === "bearish_hedge" ? "fuchsia" : "emerald"} size="xs">
            {info.row.original.is_cash_equivalent ? "cash parking" : info.getValue().replace(/_/g, " ")}
          </Badge>
        ),
      }),
      columnHelper.accessor("qty", { header: "Qty", cell: (info) => fmtNum(info.getValue()) }),
      columnHelper.accessor("avg_entry", { header: "Avg entry", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("current_price", { header: "Price", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("market_value", { header: "Market value", cell: (info) => fmtMoney(info.getValue()) }),
      columnHelper.accessor("unrealized_intraday_pnl", {
        header: "Day P&L",
        cell: (info) => info.row.original.is_cash_equivalent ? "—" : <span className={pnlClass(info.getValue())}>{fmtMoney(info.getValue())}</span>,
      }),
      columnHelper.accessor("unrealized_pnl", {
        header: "Unrealized P&L",
        cell: (info) => info.row.original.is_cash_equivalent ? "excluded" : <span className={pnlClass(info.getValue())}>{fmtMoney(info.getValue())}</span>,
      }),
      columnHelper.accessor("sector", { header: "Sector", cell: (info) => info.getValue() || "—" }),
    ] as LegacyColumnDef<PositionItem, unknown>[],
    [onSelectSymbol]
  );

  return (
    <Panel
      title="Positions"
      subtitle="Directional holdings and cash parking are identified separately. Select a symbol to inspect it."
      status={status}
      staleSince={updatedAt}
    >
      {error && !everLoaded && <StateMessage text={`Positions read failed: ${error}`} error />}
      {!error && positions.length === 0 && <StateMessage text="No open positions." />}
      {error && everLoaded && (
        <Callout title="Last known positions" color="amber" className="mb-3 !bg-panel-alt">
          As of {updatedAt?.toLocaleTimeString() || "an earlier fetch"}; fresh fetch failed ({error}).
        </Callout>
      )}
      {positions.length > 0 && (
        <DataTable
          data={positions}
          columns={columns}
          getRowId={(position) => position.symbol}
          initialSorting={[{ id: "market_value", desc: true }]}
          onRowClick={onSelectSymbol ? (position) => onSelectSymbol(position.symbol) : undefined}
        />
      )}
    </Panel>
  );
}
