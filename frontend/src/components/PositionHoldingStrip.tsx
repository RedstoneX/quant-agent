import { ReactNode } from "react";
import { Badge } from "@tremor/react";
import { OrderItem, PositionItem, TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtPct, pnlClass } from "../lib/format";
import { distanceToStop, findPositionStop } from "../lib/positionStop";
import { unrealizedPct } from "./HoldingsStrip";
import { Pill } from "./ui/Pill";

/* Compact facts for the charted holding — one wrap row above the
 * candlesticks, never a popup. Same broker-marked PositionItem data as
 * PositionsPanel, plus the protective stop reconstructed read-only from
 * open orders/trade records. Renders nothing when the symbol isn't held. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="label-xs">{label}</span>
      <span className="font-mono text-[length:var(--fs-meta)] font-semibold tabular-nums text-ink">{children}</span>
    </span>
  );
}

export function PositionHoldingStrip({
  position,
  openOrders,
  trades,
}: {
  position: PositionItem;
  openOrders: OrderItem[];
  trades: TradeItem[];
}) {
  const pct = unrealizedPct(position);
  const stop = position.is_cash_equivalent ? null : findPositionStop(position, openOrders, trades);
  const distance = stop && position.current_price != null ? distanceToStop(position.current_price, stop.price) : null;

  return (
    <div className="flex flex-shrink-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border-strong bg-panel-alt px-2.5 py-1">
      <span className="font-extrabold tracking-tight">{position.symbol}</span>
      <Pill text={position.is_cash_equivalent ? "cash parking" : position.direction} />
      <Fact label="Qty">{fmtNum(position.qty)}</Fact>
      <Fact label="Entry">{fmtMoney(position.avg_entry)}</Fact>
      <Fact label="Now">{fmtMoney(position.current_price)}</Fact>
      <Fact label="P&L">
        <span className={position.is_cash_equivalent ? "text-dim" : pnlClass(position.unrealized_pnl)}>
          {fmtMoney(position.unrealized_pnl)}
          {pct !== null ? ` (${fmtPct(pct)})` : ""}
        </span>
      </Fact>
      {!position.is_cash_equivalent && (
        stop ? (
          <>
            <Fact label="Stop">{fmtMoney(stop.price)}</Fact>
            {distance && (
              <Fact label="To stop">
                {fmtMoney(distance.amount)}
                {distance.pct !== null ? ` (${fmtPct(distance.pct)})` : ""}
              </Fact>
            )}
            <Badge color="slate" size="xs">{stop.detail}</Badge>
          </>
        ) : (
          <span className="text-[length:var(--fs-meta)] text-dim">No protective stop found</span>
        )
      )}
    </div>
  );
}
