import { Badge, Grid, Metric, Text } from "@tremor/react";
import { OrderItem, PositionItem, TradeItem } from "../api/client";
import { fmtMoney, fmtNum, fmtPct, pnlClass } from "../lib/format";
import { distanceToStop, findPositionStop } from "../lib/positionStop";
import { unrealizedPct } from "./HoldingsStrip";
import { Pill } from "./ui/Pill";

/* Owner correction (supersedes the original brief's item 2/3): the
 * holding facts for a clicked position must NOT live behind any popup,
 * modal, dialog, drawer or slide-over — nothing that has to be dismissed.
 * They belong inline, in a compact strip directly under the chart, so a
 * trader sees the chart AND the numbers at the same time with nothing
 * covering either. Every figure here is the same broker-marked
 * PositionItem data PositionsPanel/HoldingsStrip already render, plus the
 * protective stop reconstructed read-only from open orders/trade records
 * (see lib/positionStop.ts) — never a fabricated number. Renders nothing
 * when the charted symbol isn't held. */
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
    <div className="rounded-xl border border-border-strong bg-panel-alt px-3.5 py-2.5">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-[0.9rem] font-extrabold tracking-tight">{position.symbol} — open position</span>
        <Pill text={position.is_cash_equivalent ? "cash parking" : position.direction} />
      </div>
      <Grid numItems={2} numItemsSm={4} className="mt-2 gap-2.5">
        <div>
          <Text className="text-xs uppercase">Qty</Text>
          <Metric className="font-mono text-base tabular-nums text-ink">{fmtNum(position.qty)}</Metric>
        </div>
        <div>
          <Text className="text-xs uppercase">Avg entry</Text>
          <Metric className="font-mono text-base tabular-nums text-ink">{fmtMoney(position.avg_entry)}</Metric>
        </div>
        <div>
          <Text className="text-xs uppercase">Current price</Text>
          <Metric className="font-mono text-base tabular-nums text-ink">{fmtMoney(position.current_price)}</Metric>
        </div>
        <div>
          <Text className="text-xs uppercase">Unrealized P&L</Text>
          <Metric className={`font-mono text-base tabular-nums ${position.is_cash_equivalent ? "text-dim" : pnlClass(position.unrealized_pnl)}`}>
            {fmtMoney(position.unrealized_pnl)}
            {pct !== null ? ` (${fmtPct(pct)})` : ""}
          </Metric>
        </div>
      </Grid>
      {!position.is_cash_equivalent && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-border pt-2">
          {stop ? (
            <>
              <span className="flex items-baseline gap-1.5">
                <span className="label-xs">Stop</span>
                <span className="font-mono text-[length:var(--fs-stat)] font-semibold text-ink">{fmtMoney(stop.price)}</span>
              </span>
              {distance && (
                <span className="flex items-baseline gap-1.5">
                  <span className="label-xs">Distance to stop</span>
                  <span className="font-mono text-[length:var(--fs-body)] tabular-nums text-ink">
                    {fmtMoney(distance.amount)}
                    {distance.pct !== null ? ` (${fmtPct(distance.pct)})` : ""}
                  </span>
                </span>
              )}
              <Badge color="slate" size="xs">{stop.detail}</Badge>
            </>
          ) : (
            <Text className="text-xs leading-snug">
              No protective stop found — no resting stop order at the broker, and no recorded entry stop.
            </Text>
          )}
        </div>
      )}
    </div>
  );
}
