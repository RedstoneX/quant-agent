import { TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";

export function TradesPanel({ trades, error, loading }: { trades: TradeItem[]; error: string | null; loading: boolean }) {
  const status = error ? "degraded" : loading ? "loading" : "ok";
  return (
    <Panel title="Recent trades" status={status}>
      {error && <StateMessage text={`Could not load trades: ${error}`} error />}
      {!error && trades.length === 0 && <StateMessage text="No trades recorded yet." />}
      {!error && trades.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
              <th>Action</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Fill</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id}>
                <td>{fmtTime(t.timestamp)}</td>
                <td>{t.symbol}</td>
                <td>
                  <Pill text={t.action} />
                </td>
                <td>{fmtNum(t.qty)}</td>
                <td>{fmtMoney(t.price)}</td>
                <td>
                  <Pill text={t.fill_status || "unfilled"} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
