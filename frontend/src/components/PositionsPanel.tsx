import { PositionItem } from "../api/client";
import { fmtMoney, fmtNum, pnlClass } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";

export function PositionsPanel({
  positions,
  error,
  loading,
  updatedAt,
}: {
  positions: PositionItem[];
  error: string | null;
  loading: boolean;
  updatedAt?: Date | null;
}) {
  // `updatedAt` (not positions.length) is the "have we ever loaded
  // successfully" signal — an empty positions array is a legitimate loaded
  // state (no open positions), indistinguishable from "never loaded" by
  // length alone.
  const everLoaded = Boolean(updatedAt);
  const status = error ? (everLoaded ? "stale" : "error") : loading ? "loading" : "ok";
  return (
    <Panel title="Positions" status={status} staleSince={updatedAt}>
      {error && !everLoaded && <StateMessage text={`Positions read failed: ${error}`} error />}
      {!error && positions.length === 0 && <StateMessage text="No open positions." />}
      {error && everLoaded && (
        <div className="text-warn text-[0.72rem] bg-warn/10 border border-warn/30 rounded-md px-2 py-1.5 mb-2">
          Stale — last known as of {updatedAt ? updatedAt.toLocaleTimeString() : "an earlier fetch"} ({error})
        </div>
      )}
      {positions.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Direction</th>
              <th>Qty</th>
              <th>Avg Entry</th>
              <th>Price</th>
              <th>Mkt Value</th>
              <th>Unrealized P&L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td>{p.symbol}</td>
                <td>
                  <Pill text={p.direction} />
                </td>
                <td>{fmtNum(p.qty)}</td>
                <td>{fmtMoney(p.avg_entry)}</td>
                <td>{fmtMoney(p.current_price)}</td>
                <td>{fmtMoney(p.market_value)}</td>
                <td className={pnlClass(p.unrealized_pnl)}>
                  {p.is_cash_equivalent ? "—" : fmtMoney(p.unrealized_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
