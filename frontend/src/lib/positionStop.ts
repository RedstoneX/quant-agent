import { OrderItem, PositionItem, TradeItem } from "../api/client";
import { isExecutedTrade } from "./format";

/* "Where is my stop and how far am I from it" — the trader's first chart
 * question (owner's cockpit-rework brief, item 10). PositionItem itself
 * carries no stop field (see api/client.ts), so the protective stop for a
 * held symbol has to be reconstructed from two read-only sources already
 * polled elsewhere in the cockpit, in order of trust:
 *
 *   1. A resting stop-type order at the broker, on the closing side for
 *      this position (SELL for a long, BUY for a short/bearish-hedge) —
 *      this is the actual live protection right now, if one exists.
 *   2. Failing that, the most recent EXECUTED entry trade for this symbol
 *      that recorded a `stop_loss` — the intended protective level at
 *      entry, persisted alongside the fill. Labeled as a recorded
 *      intention, not proof of a live broker order (same caveat
 *      TradesPanel/StopAndExecutionTruth already state elsewhere).
 *
 * Never fabricated: a position with neither returns null, and the caller
 * must render nothing rather than invent a level.
 */

export interface PositionStop {
  price: number;
  source: "open_order" | "trade_record";
  detail: string;
}

function closingSide(position: PositionItem): "buy" | "sell" {
  // Bearish-hedge holdings are typically long an inverse instrument in
  // this fund (see styles/index.css's --c-hedge token comment); qty sign
  // is still the honest source of truth for which side closes the
  // position, long or short, without assuming direction from the label.
  return (position.qty ?? 0) < 0 ? "buy" : "sell";
}

function entryAction(position: PositionItem): "BUY" | "SELL" {
  return (position.qty ?? 0) < 0 ? "SELL" : "BUY";
}

export function findPositionStop(
  position: PositionItem,
  openOrders: OrderItem[],
  trades: TradeItem[]
): PositionStop | null {
  const side = closingSide(position);
  const restingStop = openOrders.find(
    (order) =>
      order.symbol === position.symbol &&
      (order.side || "").toLowerCase() === side &&
      (order.order_type || "").toLowerCase().includes("stop") &&
      order.stop_price != null &&
      Number.isFinite(order.stop_price)
  );
  if (restingStop && restingStop.stop_price != null) {
    return {
      price: restingStop.stop_price,
      source: "open_order",
      detail: "Resting stop order at the broker",
    };
  }

  const action = entryAction(position);
  const candidateTrades = trades
    .filter(
      (trade) =>
        trade.symbol === position.symbol &&
        trade.action === action &&
        trade.stop_loss != null &&
        Number.isFinite(trade.stop_loss) &&
        isExecutedTrade(trade)
    )
    .sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""));
  const latest = candidateTrades[0];
  if (latest && latest.stop_loss != null) {
    return {
      price: latest.stop_loss,
      source: "trade_record",
      detail: "Recorded with the most recent entry — not proof of a live broker order",
    };
  }

  return null;
}

/** The shaded risk band's [low, high] price bounds — always ordered, so a
 * short position's stop sitting ABOVE current price still shades a valid
 * (bottom, top) range rather than an inverted one. */
export function stopBandRange(currentPrice: number, stopPrice: number): { low: number; high: number } {
  return { low: Math.min(currentPrice, stopPrice), high: Math.max(currentPrice, stopPrice) };
}

/** Distance from current price to the stop, in both currency and percent
 * of current price — the two numbers a trader actually reads off "how far
 * am I from my stop." Percent is null when currentPrice is non-positive
 * (never a divide-by-zero fabrication). */
export function distanceToStop(
  currentPrice: number,
  stopPrice: number
): { amount: number; pct: number | null } {
  const amount = currentPrice - stopPrice;
  const pct = currentPrice > 0 ? (amount / currentPrice) * 100 : null;
  return { amount, pct };
}
