import { fmtMoney, fmtNum } from "./format";

export interface TradeFillDisplayFacts {
  fill_qty?: number | null;
  fill_price?: number | null;
  qty?: number | null;
  price?: number | null;
  fill_status?: string | null;
}

export function displayFillQty(trade: TradeFillDisplayFacts): string {
  return fmtNum(trade.fill_qty);
}

export function displayFillPrice(trade: TradeFillDisplayFacts): string {
  return fmtMoney(trade.fill_price);
}
