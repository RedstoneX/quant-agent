import { PositionItem } from "../api/client";
import { fmtMoney, fmtNum, pnlClass } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";

export function PositionsPanel({
  positions,
  error,
  loading,
}: {
  positions: PositionItem[];
  error: string | null;
  loading: boolean;
}) {
  const status = error ? "degraded" : loading ? "loading" : "ok";
  return (
    <Panel title="Positions" status={status}>
      {error && <StateMessage text={`Positions read failed: ${error}`} error />}
      {!error && positions.length === 0 && <StateMessage text="No open positions." />}
      {!error && positions.length > 0 && (
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
