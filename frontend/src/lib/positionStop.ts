import { OrderItem, PositionItem, PriceBar, TradeItem } from "../api/client";
import { isExecutedTrade, fmtMoney } from "./format";

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

export function entryAction(position: PositionItem): "BUY" | "SELL" {
  return (position.qty ?? 0) < 0 ? "SELL" : "BUY";
}

/** The most recent EXECUTED entry trade (BUY for a long, SELL for a
 * short/bearish-hedge, matching `entryAction`) recorded for this position's
 * symbol — the same "most recent entry" rule `findPositionStop`'s
 * trade_record fallback uses, shared here so take-profit and thesis-break
 * lookups pick the same row a stop lookup would fall back to, never a
 * different (older/newer) one for the same held symbol. Null when no
 * executed entry trade is on hand — never fabricated. */
export function latestEntryTrade(
  position: PositionItem,
  trades: TradeItem[]
): TradeItem | null {
  const action = entryAction(position);
  const candidates = trades
    .filter(
      (trade) =>
        trade.symbol === position.symbol &&
        trade.action === action &&
        isExecutedTrade(trade)
    )
    .sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""));
  return candidates[0] ?? null;
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


/* --------------------------------------------------------------------- *
 * Take-profit and thesis-break chart reference lines (chart exit-levels
 * feature). Both read the SAME `latestEntryTrade` row `findPositionStop`'s
 * trade-record fallback reads from, for the same "one held position, one
 * consistent set of levels" reason documented there.
 * --------------------------------------------------------------------- */

/** TAKE-PROFIT / TARGET reference line — straight off the entry trade's
 * own recorded `take_profit`, exactly like `positionStopLine` reads
 * `stop_loss`. Null (draw nothing) when the symbol isn't held or no
 * executed entry trade recorded a take_profit. */
export function positionTakeProfitLine(
  symbol: string | null,
  positions: PositionItem[],
  trades: TradeItem[],
  colors: { green: string }
): { price: number; color: string; title: string } | null {
  if (!symbol) return null;
  const position = positions.find((item) => item.symbol === symbol);
  if (!position) return null;
  const entry = latestEntryTrade(position, trades);
  if (!entry || entry.take_profit == null || !Number.isFinite(entry.take_profit)) {
    return null;
  }
  return {
    price: entry.take_profit,
    color: colors.green,
    title: `TARGET ${fmtMoney(entry.take_profit)}`,
  };
}

// ---------------------------------------------------------------------
// THESIS-BREAK level parsing
// ---------------------------------------------------------------------
//
// `thesis_invalid_if` is analyst free text (e.g. "Price closes below MA20
// (377.08) on above-average volume", "Price closes below the $385.70
// structural support level"). It is CONDITIONAL, not a hard line: it
// triggers on a daily CLOSE (sometimes gated on above-average volume too),
// never an intraday touch — see src/risk/exit_guard.py's
// `check_thesis_invalid_if` / `check_structural_protection`, the actual
// live checker this line is trying to visually match.
//
// THE ONE THING THAT MATTERS FOR CORRECTNESS: that checker (exit_guard.py
// ~line 1090) branches on whether the text names a moving average FIRST,
// and when it does, compares price against the LIVE ma_20/ma_50/ma_200
// recomputed that session (pipeline.py's `_structural_protection_for_
// holding`, from `compute_indicators` off the position's own fresh daily
// bars) — it NEVER reads any number written inline in the text. A number
// in parentheses/after "at" next to "MA20"/"MA50" (like the two examples
// above) is the analyst's number AT ENTRY, already stale the moment the
// average moves, and is not what the desk actually checks. So a "MA20 (X)"
// condition must plot the LIVE recomputed MA20, not X, or the chart would
// show a level the desk itself has stopped enforcing.
//
// Only a BARE price level ("closes below the $385.70 ... level", with no
// MA reference) is genuinely frozen — that IS the number the checker
// compares against, unconditionally, until the desk rewrites the row.
//
// Mirrors exit_guard.py's `_MA_REF_RE` (same three supported periods,
// same wording) — duplicated rather than imported (that regex lives in a
// Python module the frontend has no path to; kept in sync by hand, same
// as `stopBandRange`/`distanceToStop` above already duplicate no backend
// logic but this one genuinely does and must track it if it changes).
const _MA_REF_RE = /\b(?:MA|SMA|EMA)[\s-]?(\d{2,3})\b|(\d{2,3})-day\s+(?:moving\s+)?(?:average|MA|SMA|EMA)\b/i;

const _SUPPORTED_MA_PERIODS = new Set([20, 50, 200]);

export interface ThesisMaReference {
  period: 20 | 50 | 200;
}

/** The MA period (20/50/200) a thesis condition names, or null if it
 * doesn't name one of the three periods this codebase actually computes
 * (see `_SUPPORTED_MA_PERIODS` above, matching exit_guard.py exactly —
 * an MA100 or similar is deliberately left unparsed rather than guessed
 * at). */
export function parseThesisMaPeriod(text: string): ThesisMaReference | null {
  const m = _MA_REF_RE.exec(text || "");
  if (!m) return null;
  const period = Number(m[1] ?? m[2]);
  if (!_SUPPORTED_MA_PERIODS.has(period)) return null;
  return { period: period as 20 | 50 | 200 };
}

/** The single fixed price level named in `text` — used ONLY for the bare
 * "closes below the $X level" shape (no MA reference; see the module note
 * above for why an MA-referenced condition must never use this). Tries,
 * in order of confidence: a dollar-prefixed amount, a decimal number in
 * parentheses (the "(377.08)" shape) or right after "at", then a decimal
 * number next to "level"/"support"/"resistance", then a decimal number
 * right after a direction word. Returns null (never a guess) if none
 * match. */
export function parseThesisBreakLevel(text: string): number | null {
  const clean = (raw: string) => Number(raw.replace(/,/g, ""));
  const patterns: RegExp[] = [
    /\$\s?([\d,]+(?:\.\d+)?)/, // dollar-prefixed: "$385.70"
    /\(\s*\$?([\d,]+\.\d+)\s*\)/, // parenthetical: "(377.08)"
    /\bat\s+\$?([\d,]+\.\d+)\b/i, // "at 341.21"
    /([\d,]+\.\d+)\s*(?:level|support|resistance)\b/i,
    /(?:support|resistance|level)\D{0,15}?([\d,]+\.\d+)/i,
    /\b(?:above|below|over|under)\b\D{0,12}?([\d,]+\.\d+)/i,
  ];
  for (const re of patterns) {
    const m = re.exec(text || "");
    if (m && m[1]) {
      const value = clean(m[1]);
      if (Number.isFinite(value)) return value;
    }
  }
  return null;
}

/** Simple moving average over the last `period` daily closes — the exact
 * same rolling-mean math `src/data/technical.py::compute_indicators` uses
 * for `ma_20`/`ma_50`/`ma_200` (`df["close"].rolling(period).mean()`),
 * recomputed here from the chart's own already-loaded daily bars rather
 * than duplicated state, so it reads live. Requires at least `period`
 * bars (matching that function's own `len(df) >= period` gate) — null,
 * never a partial/short-window average, when there aren't enough. */
export function computeSMA(bars: PriceBar[], period: number): number | null {
  if (bars.length < period) return null;
  const window = bars.slice(bars.length - period);
  const sum = window.reduce((acc, bar) => acc + bar.close, 0);
  return sum / period;
}

export interface ThesisBreakLine {
  price: number;
  color: string;
  title: string;
  /** True when `price` is a live-recomputed MA (drifts with each new daily
   * close) rather than a frozen number read out of the analyst's text. */
  live: boolean;
}

/** THESIS-BREAK reference line. See the module note above for why an
 * MA-referenced condition plots the LIVE recomputed average (matching
 * what the desk's own `check_thesis_invalid_if` actually enforces) while
 * a bare price-level condition plots the frozen number from the text.
 *
 * `dailyBars` MUST be daily-close bars (the chart's `1d` timeframe series)
 * — `compute_indicators` computes ma_20/50/200 off daily closes only, so
 * feeding this an intraday series would silently compute a different,
 * wrong quantity. Callers pass null/empty when the chart isn't currently
 * showing daily bars; this then draws nothing for an MA-referenced
 * condition rather than show a number that doesn't match what the desk
 * enforces (never fabricated, never approximated to "close enough").
 *
 * Returns null (draw nothing) when: the symbol isn't held, there's no
 * recorded thesis_invalid_if, no numeric level or supported MA period can
 * be identified in the text, or an MA period WAS identified but there
 * aren't enough daily bars on hand yet to compute it live. */
export function positionThesisBreakLine(
  symbol: string | null,
  positions: PositionItem[],
  trades: TradeItem[],
  dailyBars: PriceBar[] | null,
  colors: { agent: string }
): ThesisBreakLine | null {
  if (!symbol) return null;
  const position = positions.find((item) => item.symbol === symbol);
  if (!position) return null;
  const entry = latestEntryTrade(position, trades);
  const text = (entry?.thesis_invalid_if || "").trim();
  if (!text) return null;

  const maRef = parseThesisMaPeriod(text);
  if (maRef) {
    const live = dailyBars ? computeSMA(dailyBars, maRef.period) : null;
    if (live == null) return null; // not enough daily bars on hand — no guess
    return {
      price: live,
      color: colors.agent,
      title: `THESIS-BREAK (cond.) MA${maRef.period} ${fmtMoney(live)}`,
      live: true,
    };
  }

  const level = parseThesisBreakLevel(text);
  if (level == null) return null;
  return {
    price: level,
    color: colors.agent,
    title: `THESIS-BREAK (cond.) ${fmtMoney(level)}`,
    live: false,
  };
}
