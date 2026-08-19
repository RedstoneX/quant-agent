import { useState } from "react";
import { OrderItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";

export function OrdersPanel({
  orders,
  error,
  loading,
  status: orderStatus,
  onStatusChange,
}: {
  orders: OrderItem[];
  error: string | null;
  loading: boolean;
  status: string;
  onStatusChange: (status: "open" | "closed" | "all") => void;
}) {
  const panelStatus = error ? "degraded" : loading ? "loading" : "ok";
  return (
    <Panel
      title="Orders"
      status={panelStatus}
      actions={
        <select
          value={orderStatus}
          onChange={(e) => onStatusChange(e.target.value as "open" | "closed" | "all")}
          className="bg-panel-alt border border-border rounded text-[0.78rem] px-1.5 py-0.5"
        >
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="all">All</option>
        </select>
      }
    >
      {error && <StateMessage text={`Orders read failed: ${error}`} error />}
      {!error && orders.length === 0 && <StateMessage text={`No ${orderStatus} orders.`} />}
      {!error && orders.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Status</th>
              <th>Limit/Stop</th>
              <th>Filled</th>
              <th>Submitted</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td>{o.symbol}</td>
                <td>{(o.side || "—").toUpperCase()}</td>
                <td>{fmtNum(o.qty)}</td>
                <td>
                  <Pill text={o.status} />
                </td>
                <td>{fmtMoney(o.limit_price ?? o.stop_price)}</td>
                <td>
                  {fmtNum(o.filled_qty)} @ {fmtMoney(o.filled_avg_price)}
                </td>
                <td>{fmtTime(o.submitted_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

export function useOrderStatus() {
  return useState<"open" | "closed" | "all">("open");
}
