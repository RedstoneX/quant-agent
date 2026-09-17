import { OrderItem, TradeItem } from "../api/client";

/** Recorded take-profit for an order, or null.

The broker order itself has no take-profit field (the desk does not send
bracket targets). The number lives on the trade record: the matching
`broker_order_id`, or — for a protective stop that is a different order —
the single unique take-profit already recorded for that symbol. Two
different recorded targets for the same symbol are not collapsed; that
would be guessing. */
export function recordedOrderTarget(order: OrderItem, trades: TradeItem[]): number | null {
  const linked = trades.find((trade) => trade.broker_order_id === order.id);
  if (linked?.take_profit != null) return linked.take_profit;
  const unique = [
    ...new Set(
      trades
        .filter((trade) => trade.symbol === order.symbol && trade.take_profit != null)
        .map((trade) => trade.take_profit as number),
    ),
  ];
  return unique.length === 1 ? unique[0] : null;
}
