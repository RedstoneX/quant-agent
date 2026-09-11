import { MouseEvent as ReactMouseEvent, useEffect, useRef, useState } from "react";
import {
  createChart,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  CandlestickData,
  HistogramData,
  LineStyle,
  LogicalRange,
  SeriesMarker,
  TickMarkType,
  Time,
} from "lightweight-charts";
import { Button } from "@tremor/react";
import {
  api, ChartTimeframe, DividendEvent, EarningsEvent, LiveQuote, OrderItem,
  PositionItem, PriceBar, TradeItem,
} from "../api/client";
import { Panel } from "./ui/Panel";
import { isExecutedTrade, etDateKey, fmtMoney, fmtNum } from "../lib/format";
import { usePoll } from "../lib/usePoll";
import { findPositionStop } from "../lib/positionStop";

// Theme vars are space-separated "R G B" (Tailwind's arbitrary-alpha
// convention, valid modern CSS) — lightweight-charts' internal color
// parser only accepts the classic comma-separated rgb()/rgba() syntax,
// so every color handed to the chart is built from the raw components
// here rather than by string-concatenating onto a CSS color value.
function readThemeColors() {
  const style = getComputedStyle(document.documentElement);
  const rgb = (name: string) => style.getPropertyValue(name).trim().split(/\s+/).join(", ");
  const solid = (components: string) => `rgb(${components})`;
  const alpha = (components: string, a: number) => `rgba(${components}, ${a})`;
  const text = rgb("--c-ink-dim");
  const border = rgb("--c-border");
  const green = rgb("--c-green");
  const red = rgb("--c-red");
  const accent = rgb("--c-accent");
  const amber = rgb("--c-amber");
  // Dividend/earnings chart markers (item: corporate-action markers) reuse
  // the existing --c-agent token (purple) rather than introducing a new
  // CSS color — it carries no meaning elsewhere on THIS panel (entry uses
  // accent/blue, stop uses amber, P&L uses green/red), and purple vs. blue
  // is a colorblind-safe pairing for the owner's red-green color
  // vision (see CLAUDE.md), unlike anything bordering red/orange/green.
  const agent = rgb("--c-agent");
  return {
    text: solid(text),
    border: solid(border),
    green: solid(green),
    red: solid(red),
    accent: solid(accent),
    amber: solid(amber),
    agent: solid(agent),
    greenAlpha: alpha(green, 0.35),
    redAlpha: alpha(red, 0.35),
    // The live-price line is deliberately muted (item 4 of the cockpit
    // trader rework): a thin dashed line in the same dim ink used for
    // axis/grid text, not the bright accent color, so it never reads as
    // more important than the candles or the trader's own entry/stop
    // lines. See livePriceLineRef below.
    textMuted: alpha(text, 0.6),
    // Upcoming (not-yet-reported) earnings are the SAME hue as a past
    // report, just faded — a lightness/alpha distinction reads correctly
    // regardless of color vision, rather than relying on a second hue that
    // could land near red/green/amber by accident.
    agentFaded: alpha(agent, 0.45),
  };
}

// Lightweight Charts has no built-in notion of the market's own timezone —
// left unconfigured, it renders every `Time` value (axis tick labels, the
// crosshair time label, and anything this file itself formats from a raw
// epoch/timestamp) using raw UTC clock digits. QAMC's market is US Eastern,
// so every DISPLAYED time on this panel must read in ET instead, and must
// track the EDT/EST transition automatically rather than assume a fixed
// offset — Intl.DateTimeFormat with timeZone: "America/New_York" handles
// that transition correctly on its own. This never touches the underlying
// Time values handed to the chart series (those stay UTC epoch seconds,
// which lightweight-charts requires internally) — only how a moment is
// FORMATTED for a human to read. Shared by the tick-mark/crosshair
// formatters below and by every other time-of-day string this file builds
// (the "bars through HH:MM" and "as of HH:MM" subtitle text).
function formatEasternTime(input: Date | number, options: Intl.DateTimeFormatOptions): string {
  const date = typeof input === "number" ? new Date(input * 1000) : input;
  return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", ...options }).format(date);
}

// lightweight-charts' TickMarkFormatter is handed the raw axis `Time` plus
// which granularity it's labeling (year/month/day/time-of-day); returning
// null defers to the library's own default formatting. Daily-timeframe
// ticks carry a date-only business-day value (see barTime below) with no
// time-of-day component, so there's no UTC-vs-ET ambiguity to fix there —
// only the numeric (UTC epoch seconds) ticks used by every intraday
// timeframe need to be reformatted in ET.
function easternTickMarkFormatter(time: Time, tickMarkType: TickMarkType): string | null {
  if (typeof time !== "number") return null;
  switch (tickMarkType) {
    case TickMarkType.Year:
      return formatEasternTime(time, { year: "numeric" });
    case TickMarkType.Month:
      return formatEasternTime(time, { month: "short" });
    case TickMarkType.DayOfMonth:
      return formatEasternTime(time, { month: "short", day: "numeric" });
    case TickMarkType.TimeWithSeconds:
      return formatEasternTime(time, { hour: "numeric", minute: "2-digit", second: "2-digit" });
    case TickMarkType.Time:
    default:
      return formatEasternTime(time, { hour: "numeric", minute: "2-digit" });
  }
}

function barTime(bar: PriceBar, timeframe: ChartTimeframe): Time {
  if (timeframe === "1d" || !bar.timestamp) return bar.date as Time;
  return Math.floor(new Date(bar.timestamp).getTime() / 1000) as Time;
}

function toCandles(bars: PriceBar[], timeframe: ChartTimeframe): CandlestickData[] {
  return bars.map((b) => ({
    time: barTime(b, timeframe), open: b.open, high: b.high,
    low: b.low, close: b.close,
  }));
}

/** Add/replace today's still-forming daily candle only when the quote
 * supplies a complete, non-fabricated OHLC set. The live price line is
 * rendered independently, so partial snapshots still show current price. */
export function chartCandles(
  bars: PriceBar[],
  quote: LiveQuote | null,
  today = etDateKey(new Date()),
  timeframe: ChartTimeframe = "1d"
): CandlestickData[] {
  const candles = toCandles(bars, timeframe);
  if (timeframe !== "1d") return candles;
  const values = [quote?.session_open, quote?.session_high, quote?.session_low, quote?.last_price];
  if (!today || values.some((value) => value == null || !Number.isFinite(value))) return candles;

  const open = quote!.session_open!;
  const close = quote!.last_price!;
  const high = Math.max(quote!.session_high!, open, close);
  const low = Math.min(quote!.session_low!, open, close);
  const forming: CandlestickData = { time: today, open, high, low, close };
  const todayIndex = candles.findIndex((candle) => candle.time === today);
  if (todayIndex >= 0) candles[todayIndex] = forming;
  else if (!candles.length || String(candles[candles.length - 1].time) < today) candles.push(forming);
  return candles;
}

function toVolume(
  bars: PriceBar[],
  timeframe: ChartTimeframe,
  colors: { greenAlpha: string; redAlpha: string }
): HistogramData[] {
  return bars.map((b) => ({
    time: barTime(b, timeframe),
    value: b.volume,
    color: b.close >= b.open ? colors.greenAlpha : colors.redAlpha,
  }));
}

// TradeItem.timestamp is naive UTC. Daily markers use the run's ET trading
// date; intraday markers snap backward to the exact candle that contains
// the fill, since Lightweight Charts only renders markers on existing data
// points (a 13:34:46 fill belongs on the 13:30 five-minute candle). Shared
// by tradeMarkers (below) and entryTimeMarkers, so both a general BUY/SELL
// execution marker and an entry-specific marker land on the same bar for
// the same timestamp — one bucketing convention, not two.
function markerTimeForTimestamp(
  timestamp: string,
  timeframe: ChartTimeframe,
  intradayTimes: number[]
): Time | null {
  if (timeframe === "1d") return etDateKey(timestamp) as Time;
  const fillSeconds = Math.floor(
    new Date(timestamp.endsWith("Z") || timestamp.includes("+") ? timestamp : `${timestamp}Z`).getTime() / 1000
  );
  if (!Number.isFinite(fillSeconds)) return null;
  let containing: number | null = null;
  for (const time of intradayTimes) {
    if (time > fillSeconds) break;
    containing = time;
  }
  return containing as Time | null;
}

export function tradeMarkers(
  symbol: string,
  trades: TradeItem[],
  colors: { green: string; red: string },
  timeframe: ChartTimeframe,
  availableTimes: Time[]
): SeriesMarker<Time>[] {
  const intradayTimes = availableTimes
    .filter((time) => typeof time === "number")
    .map(Number)
    .sort((a, b) => a - b);

  const markerTime = (timestamp: string): Time | null =>
    markerTimeForTimestamp(timestamp, timeframe, intradayTimes);

  return trades
    .filter((t) => t.symbol === symbol && t.timestamp && isExecutedTrade(t) && (t.action === "BUY" || t.action === "SELL"))
    .flatMap((t): SeriesMarker<Time>[] => {
      const time = markerTime(t.timestamp!);
      const quantity = t.fill_qty ?? t.qty;
      return time == null ? [] : [{
        time,
        position: t.action === "BUY" ? "belowBar" : "aboveBar",
        color: t.action === "BUY" ? colors.green : colors.red,
        shape: t.action === "BUY" ? "arrowUp" : "arrowDown",
        text: `${t.action}${quantity ? ` ${quantity}` : ""}`,
      }];
    })
    .sort((a, b) => String(a.time).localeCompare(String(b.time)));
}

/* "Where am I versus the market" — the trader's own average entry for the
 * charted symbol, drawn as a labelled horizontal line with its live
 * unrealized P&L. Sourced from the same broker-marked PositionItem the
 * Positions panel and the holdings strip render; never inferred from the
 * bar data. Returns null when the symbol is not held (or the entry price
 * is missing/zero), in which case nothing is drawn at all — an absent
 * position must never produce a line at 0.
 *
 * Cash-parking rows (SGOV) are included deliberately: if the operator
 * charts the sweep instrument, its real average entry is still the honest
 * answer to "where am I". */
export function entryPriceLine(
  symbol: string | null,
  positions: PositionItem[],
  colors: { green: string; red: string }
): { price: number; color: string; title: string } | null {
  if (!symbol) return null;
  const position = positions.find((item) => item.symbol === symbol);
  if (!position) return null;
  const price = position.avg_entry;
  if (price == null || !Number.isFinite(price) || price <= 0) return null;
  const pnl = position.unrealized_pnl;
  const pnlText = pnl == null || !Number.isFinite(pnl) ? "" : ` · ${pnl >= 0 ? "+" : ""}${fmtMoney(pnl)}`;
  return {
    price,
    // Market truth keeps the market-truth palette: green when the
    // position is up, red when it is down (never the cyan system accent,
    // which is reserved for chrome — see styles/index.css's token grammar).
    color: (pnl ?? 0) < 0 ? colors.red : colors.green,
    title: `ENTRY ${fmtNum(position.qty)} @ ${fmtMoney(price)}${pnlText}`,
  };
}

/* Blue entry-TIME marker — additive to entryPriceLine above, which draws
 * the horizontal ENTRY price line: this places a small upward triangle on
 * the specific bar where the entry actually happened. PositionItem carries
 * no timestamp (see positionStop.ts's comment on the same gap for the
 * stop), so the entry moment is reconstructed the same way
 * findPositionStop reconstructs the stop level: the most recent EXECUTED
 * trade for this symbol whose action matches how the held position would
 * have been entered (BUY for long, SELL for short/bearish-hedge, by qty
 * sign). Uses the same bar-bucketing markerTimeForTimestamp already uses
 * for BUY/SELL execution arrows, so a fine (5m) chart snaps to the exact
 * containing candle and a coarse (1D) chart lands on that day's bar —
 * nothing timeframe-specific added here. One marker per held position for
 * the charted symbol (normally one; not assumed to be exactly one).
 * Returns [] when no matching entry trade exists — never a fabricated
 * timestamp. */
export function entryTimeMarkers(
  symbol: string | null,
  positions: PositionItem[],
  trades: TradeItem[],
  color: string,
  timeframe: ChartTimeframe,
  availableTimes: Time[]
): SeriesMarker<Time>[] {
  if (!symbol) return [];
  const held = positions.filter((item) => item.symbol === symbol);
  if (!held.length) return [];
  const intradayTimes = availableTimes
    .filter((time) => typeof time === "number")
    .map(Number)
    .sort((a, b) => a - b);

  return held.flatMap((position): SeriesMarker<Time>[] => {
    const action = (position.qty ?? 0) < 0 ? "SELL" : "BUY";
    const entryTrade = trades
      .filter(
        (t) =>
          t.symbol === symbol &&
          t.action === action &&
          t.timestamp &&
          isExecutedTrade(t)
      )
      .sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""))[0];
    if (!entryTrade?.timestamp) return [];
    const time = markerTimeForTimestamp(entryTrade.timestamp, timeframe, intradayTimes);
    return time == null
      ? []
      : [{ time, position: "belowBar", color, shape: "arrowUp", text: "ENTRY" }];
  });
}

// Date-only event (a dividend ex-date or an earnings-report date, neither
// of which carries a time-of-day) bucketed onto the chart using the same
// day-bucketing convention markerTimeForTimestamp already uses for
// timestamped trade/entry markers above: on the daily timeframe the event's
// own date IS the bar time; on an intraday timeframe there is no fill time
// to snap to, so this looks for whichever loaded intraday bar falls on that
// same ET calendar day (its first one) and returns null — draw nothing —
// when the date isn't in the currently-loaded/visible bars at all, exactly
// as the task calls for ("just show if the date happens to be in the
// visible intraday range").
function eventMarkerTime(dateKey: string, timeframe: ChartTimeframe, availableTimes: Time[]): Time | null {
  if (timeframe === "1d") return dateKey as Time;
  const intradayTimes = availableTimes.filter((t) => typeof t === "number").map(Number).sort((a, b) => a - b);
  const match = intradayTimes.find((t) => etDateKey(new Date(t * 1000)) === dateKey);
  return match == null ? null : (match as Time);
}

/* Dividend/earnings markers — investigated per the owner's request before
 * building anything new: yfinance (already a dependency, already wrapped by
 * MarketDataProvider for ex-div/next-earnings risk signals) is the data
 * source; see src/data/market.py's get_price_chart_events and the
 * /events/{symbol} route. A round marker per ex-dividend date; a square
 * marker per earnings-report date, colored distinctly for already-reported
 * vs. upcoming/estimated (see readThemeColors' agent/agentFaded — a
 * colorblind-safe hue the rest of this panel doesn't otherwise use).
 * Returns [] on empty input, same "never fabricate" contract as every
 * other marker builder in this file. */
// Fix (owner re-test, 2026-09-10): `position: "belowBar"` was the previous
// attempt at this, and it is NOT what it looks like — lightweight-charts
// anchors a "belowBar" SeriesMarker to that specific candle's LOW price,
// not to any fixed screen row, so on a volatile day the marker floats up
// and down with the candle body exactly like an "aboveBar" one would,
// which is what the owner caught on re-test. There is no series-marker
// option that means "fixed row on screen" — the library's marker system is
// price-anchored, full stop. So dividend/earnings markers are no longer
// SeriesMarkers at all: these two builders now return plain data (a time +
// kind + color + title, no lightweight-charts type), and the component
// below converts each item's time to an X pixel via
// timeScale().timeToCoordinate() and renders it as an absolutely
// positioned HTML icon in a fixed-height strip just above the time axis —
// see eventOverlayRow state/recomputeEventOverlayPositions. This is
// exactly TradingView's own real behavior (a persistent corporate-action
// row decoupled from price), which the owner asked for by name. The BUY/
// SELL trade markers and the blue entry-time marker are UNCHANGED — they
// stay correctly price-anchored native SeriesMarkers (tradeMarkers/
// entryTimeMarkers above); only dividend/earnings moved off the price
// series entirely. Round=dividend vs. square=earnings and the past-vs-
// upcoming color distinction (agent vs. agentFaded) are unchanged from
// before.
export type EventOverlayItem = {
  time: Time;
  kind: "dividend" | "earnings";
  color: string;
  title: string;
};

export function dividendOverlayItems(
  dividends: DividendEvent[],
  color: string,
  timeframe: ChartTimeframe,
  availableTimes: Time[]
): EventOverlayItem[] {
  return dividends.flatMap((d): EventOverlayItem[] => {
    const time = eventMarkerTime(d.date, timeframe, availableTimes);
    if (time == null) return [];
    return [{
      time,
      kind: "dividend",
      color,
      title: `Dividend · ${d.date}${d.amount != null ? ` · $${d.amount}` : ""}`,
    }];
  });
}

export function earningsOverlayItems(
  earnings: EarningsEvent[],
  colors: { past: string; upcoming: string },
  timeframe: ChartTimeframe,
  availableTimes: Time[]
): EventOverlayItem[] {
  return earnings.flatMap((e): EventOverlayItem[] => {
    const time = eventMarkerTime(e.date, timeframe, availableTimes);
    if (time == null) return [];
    return [{
      time,
      kind: "earnings",
      color: e.upcoming ? colors.upcoming : colors.past,
      title: `Earnings · ${e.date}${e.upcoming ? " (upcoming)" : ""}`,
    }];
  });
}

/* "Where is my stop and how far am I from it" — item 10 of the cockpit
 * trader rework. Sourced from findPositionStop (lib/positionStop.ts): a
 * resting stop order at the broker when one exists, else the most recent
 * entry trade's recorded stop. Deliberately amber/warn, not the money-
 * direction green/red the entry line uses — a stop is a planned risk
 * boundary, not a live gain/loss fact (see styles/index.css's token
 * grammar and item 12). Returns null (draws nothing) when the symbol
 * isn't held or no stop evidence exists — never a fabricated level. */
export function positionStopLine(
  symbol: string | null,
  positions: PositionItem[],
  openOrders: OrderItem[],
  trades: TradeItem[],
  colors: { amber: string }
): { price: number; color: string; title: string } | null {
  if (!symbol) return null;
  const position = positions.find((item) => item.symbol === symbol);
  if (!position) return null;
  const stop = findPositionStop(position, openOrders, trades);
  if (!stop) return null;
  return {
    price: stop.price,
    color: colors.amber,
    title: `STOP ${fmtMoney(stop.price)}${stop.source === "open_order" ? "" : " (recorded)"}`,
  };
}

// The chart must always resize to a real, non-trivial height even on a
// short/laptop viewport where `calc(100vh-150px)` leaves less room than a
// tall desktop monitor — never so short the candles become unreadable.
// Matches the container's own `min-h-[280px]` floor below.
const MIN_CHART_HEIGHT = 280;
// Dividend/earnings bottom-row marker size (owner re-test, 2026-09-10,
// round 2 fix) — matches TradingView's own real marker size (roughly
// 14-16px), large enough for the "D"/"E" letter drawn inside it to stay
// legible at --fs-micro-adjacent (10px) font size. See the marker row JSX
// below for the letter/tooltip fix this constant belongs to.
//
// Bumped 16 -> 20 (owner re-test, round 3, 2026-09-10): confirmed correctly
// sized per this constant but still read as "small" and as "buried a
// little bit below the [LIVE price] line" against the real chart. 20px
// matches TradingView's marker size at the upper end of its own range
// rather than the lower end.
const MARKER_SIZE = 20;
const TIMEFRAMES: Array<{ value: ChartTimeframe; label: string; lookbackDays: number }> = [
  { value: "5m", label: "5m", lookbackDays: 1 },
  { value: "15m", label: "15m", lookbackDays: 5 },
  { value: "1h", label: "1h", lookbackDays: 30 },
  { value: "1d", label: "1D", lookbackDays: 120 },
];

// Fix: "panning left past the fetched range shows blank." Bars are loaded
// once per [symbol, timeframe] with a fixed lookback (TIMEFRAMES above);
// these govern the guarded fetch-more-on-pan behavior below. The backend
// /prices endpoint no longer caps lookback_days (the old 500-day cap was
// removed), so "fetch more" just means "re-fetch the same symbol/timeframe
// with a bigger lookback_days and swap in the wider result" — not
// incremental pagination. There is deliberately no hardcoded day ceiling
// here to match: doubling continues until the backend itself stops
// returning more bars (see the `addedCount <= 0` check in
// fetchMoreHistory, and historyExhaustedRef below), which is the real
// data floor (Alpaca/IEX history start) rather than an arbitrary frontend
// number that would just recreate the bug this fixes.
// How close (in bars) the visible range's left edge has to get to the
// start of the currently-loaded series before we treat it as "the operator
// is panning toward the edge" and fetch further history.
const FETCH_MORE_EDGE_BARS = 10;

export function PriceChartPanel({
  symbol,
  trades = [],
  positions = [],
  openOrders = [],
  positionTrades,
  onUserInteraction,
}: {
  symbol: string | null;
  /** Drives the BUY/SELL execution arrows on the price series — typically
   * scoped to whichever session is currently selected elsewhere in the
   * cockpit. */
  trades?: TradeItem[];
  /* Live broker positions — used to draw the operator's own average entry
   * (see entryPriceLine above) and protective stop (see positionStopLine
   * above) for the charted symbol. Read-only, like everything else on
   * this surface. */
  positions?: PositionItem[];
  /* Open broker orders — used only to find a resting stop order for the
   * charted symbol (see positionStopLine/findPositionStop). Not the same
   * list the Orders panel's status filter shows; always "open" regardless
   * of what filter an operator has picked there. */
  openOrders?: OrderItem[];
  /** Trades used ONLY as the trade-record fallback for the protective-stop
   * lookup (see positionStopLine) — deliberately separate from `trades`
   * above. A held position may have been entered in an earlier session
   * than whichever one is currently selected, so this defaults to the
   * broader recent-trades list rather than reusing the session-scoped
   * `trades` prop, which would silently miss it. Falls back to `trades`
   * itself when not given. */
  positionTrades?: TradeItem[];
  /** Fired on any interaction the operator makes directly on this panel —
   * pan/zoom, a timeframe click, Reset zoom — so App.tsx can tell auto-
   * follow to back off while the operator is actively engaged (see
   * App.tsx's chart-engagement tracking). Never fired for this panel's own
   * programmatic range changes (initial fit, resize, symbol/timeframe
   * switch, fetch-more-on-pan's view restore) — see suppressInteractionRef
   * below. */
  onUserInteraction?: () => void;
}) {
  const stopLookupTrades = positionTrades ?? trades;
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const livePriceLineRef = useRef<IPriceLine | null>(null);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const stopLineRef = useRef<IPriceLine | null>(null);
  // Latest onUserInteraction, read from the mount-once chart effect and the
  // range-change subscription set up inside it — a ref so those don't need
  // symbol/timeframe-style dependency wiring just to see a fresh callback.
  const onUserInteractionRef = useRef(onUserInteraction);
  onUserInteractionRef.current = onUserInteraction;
  // True while this panel is itself driving a visible-range change
  // (initial fit, resize-triggered fit, symbol/timeframe switch, or the
  // fetch-more-on-pan view restore below) — the subscribeVisibleLogicalRangeChange
  // handler skips onUserInteraction while this is set, so this panel's own
  // programmatic moves are never mistaken for the operator panning/zooming.
  const suppressInteractionRef = useRef(false);
  // Fetch-more-on-pan bookkeeping (Fix: blank history when panning left).
  // Refs, not state — read from the chart's own event handler, which is
  // wired up once at mount and must always see current values. Initialized
  // with literal defaults (not `symbol`/`timeframe`, declared below) and
  // kept current by the plain assignments right after those useState calls.
  const symbolRef = useRef<string | null>(null);
  const timeframeRef = useRef<ChartTimeframe>("1d");
  const barsRef = useRef<PriceBar[]>([]);
  const lookbackDaysRef = useRef(TIMEFRAMES[TIMEFRAMES.length - 1].lookbackDays);
  const fetchingMoreRef = useRef(false);
  // Set once a fetch-more attempt comes back with no additional bars over
  // what was already loaded — the real signal that we've reached the
  // backend's actual data floor (Alpaca/IEX history start), not a guessed
  // day count. Stops the pan handler from re-issuing an identical, always-
  // empty "fetch more" request on every subsequent pan tick at the edge.
  // Reset alongside fetchingMoreRef on every genuine symbol/timeframe
  // switch, since a new symbol may have a different real history depth.
  const historyExhaustedRef = useRef(false);
  // Set by fetchMoreHistory just before its setBars(); consumed by the
  // render effect right after it applies the merged data to the series, so
  // the visible range is restored in the same synchronous pass that
  // prepended the older bars — never a race against React's own scheduling.
  const pendingRangeRestoreRef = useRef<{ range: LogicalRange; addedCount: number } | null>(null);
  // Bug fix: a genuine symbol/timeframe switch needs fitContent() called
  // AFTER the new bars actually land in the series (via setData() in the
  // render effect below), not before. lightweight-charts does not
  // auto-fit on setData() once the time scale has ever had an explicit
  // range set — calling fitContent() early (before the new data arrives,
  // against the stale previous series) locks in a range that the new
  // data then renders into unchanged, which is exactly "blank chart" /
  // "chart stuck showing the old window". Set here when a switch starts,
  // consumed once by the render effect right after setData() so it never
  // fires again on a same-symbol quote poll or a fetch-more update (those
  // use pendingRangeRestoreRef instead, which must win over this one).
  const needsInitialFitRef = useRef(false);
  // Mirrors `loading` for the bars-poll below — a ref so its usePoll
  // closure (subscribed once per [symbol, timeframe], same as the quote
  // poll) always sees the current value instead of the one captured when
  // the interval was set up.
  const loadingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [barCount, setBarCount] = useState(0);
  const [bars, setBars] = useState<PriceBar[]>([]);
  // TradingView-style bottom range slider (item: horizontal time-navigation
  // bar). Mirrors the chart's own visible logical range — read from the
  // same subscribeVisibleLogicalRangeChange subscription the fetch-more-on-
  // pan/auto-follow-suppression logic above already uses, written back to
  // the chart via setVisibleLogicalRange on drag. State (not a ref): the
  // slider's own rendered position needs to re-render on every range
  // change, unlike the refs above which only feed event-handler logic.
  const [visibleRange, setVisibleRangeState] = useState<LogicalRange | null>(null);
  const sliderTrackRef = useRef<HTMLDivElement>(null);
  // Drag bookkeeping for the slider's window handle — a ref, not state:
  // read/written only from the document-level mousemove/mouseup listeners
  // set up in onSliderWindowMouseDown, never needs to trigger a render
  // itself (the chart's own range-change subscription is what drives the
  // re-render, via visibleRange above).
  const sliderDragRef = useRef<{ startX: number; startFrom: number; startTo: number; trackWidth: number } | null>(
    null
  );
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("1d");
  // Keep the refs above current every render — plain assignments (not
  // effects) so the mount-once chart effect and its event handlers always
  // see this render's latest symbol/timeframe/bars without needing to be
  // re-subscribed.
  symbolRef.current = symbol;
  timeframeRef.current = timeframe;
  barsRef.current = bars;
  loadingRef.current = loading;
  // Last historical bar's date — used only to tell the operator the chart
  // is running behind today (during market hours the daily bar for "today"
  // isn't a completed historical bar yet), never to infer a current price.
  const [lastBarTime, setLastBarTime] = useState<string | null>(null);
  // Genuinely live quote (GET /quotes), deliberately a separate fetch/state
  // from the historical bars above — see docs/architecture/
  // MISSION_CONTROL_API.md "Mission Control data-truth" tranche. Stale-not-
  // blank: a failed refresh keeps the last-known quote on screen, tagged
  // with its own fetch error, rather than silently showing nothing.
  const [quote, setQuote] = useState<LiveQuote | null>(null);
  const [quoteAsOf, setQuoteAsOf] = useState<Date | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  // Dividend/earnings markers — GET /events, fetched once per symbol (not
  // polled: these dates don't change tick-to-tick like a quote). A failed
  // fetch just means no markers render for this symbol; never blocks the
  // rest of the chart, matching every other best-effort overlay here.
  const [dividendEvents, setDividendEvents] = useState<DividendEvent[]>([]);
  const [earningsEvents, setEarningsEvents] = useState<EarningsEvent[]>([]);

  useEffect(() => {
    if (!containerRef.current) return;
    const colors = readThemeColors();
    const initialWidth = containerRef.current.clientWidth || 600;
    const initialHeight = Math.max(containerRef.current.clientHeight, MIN_CHART_HEIGHT);
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "transparent" }, textColor: colors.text, fontSize: 11 },
      grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
      rightPriceScale: { borderColor: colors.border },
      // Both the axis tick labels and the crosshair time label must read in
      // ET, not the library's raw-UTC default — see easternTickMarkFormatter/
      // formatEasternTime above.
      localization: {
        timeFormatter: (time: Time) =>
          typeof time === "number" ? formatEasternTime(time, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : String(time),
      },
      timeScale: { borderColor: colors.border, tickMarkFormatter: easternTickMarkFormatter },
      width: initialWidth,
      height: initialHeight,
      // Fix: "mouse wheel over the chart is fully captured for zoom, so the
      // page never scrolls" — lightweight-charts' own mouseWheel handling
      // (both handleScale's zoom-on-wheel and handleScroll's scroll-on-
      // wheel) is turned off here; a plain wheel event is then left
      // untouched by the manual listener below (no preventDefault), so the
      // browser scrolls the page exactly as it would over any other
      // element. Ctrl/Cmd+wheel re-implements zoom manually (see the
      // "wheel" listener below) — the common modifier-to-zoom pattern
      // (Google Maps, TradingView) rather than the library's own always-on
      // wheel capture.
      handleScroll: { mouseWheel: false },
      handleScale: { mouseWheel: false },
    });
    const candleSeries = chart.addCandlestickSeries({
      upColor: colors.green,
      downColor: colors.red,
      borderVisible: false,
      wickUpColor: colors.green,
      wickDownColor: colors.red,
      // The series' default last-value line would label the final
      // historical close as if it were current. Only the explicitly
      // sourced LIVE line below may make that claim.
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    // Fixed dividend/earnings row: converts each item's bar-time to an X
    // pixel via timeScale().timeToCoordinate() (null for a time currently
    // scrolled out of view — those are simply dropped, not drawn at a
    // clamped edge). Re-run below on every visible-range change and every
    // resize, so the row tracks pan/zoom/slider-drag exactly like the
    // chart itself; also invoked directly whenever eventOverlayItems
    // changes (new symbol/events loaded).
    const recomputeEventOverlayPositions = () => {
      const ts = chart.timeScale();
      const positioned = eventOverlayItemsRef.current.flatMap((item) => {
        const x = ts.timeToCoordinate(item.time);
        if (x == null) return [];
        return [{ x, kind: item.kind, color: item.color, title: item.title }];
      });
      setEventOverlayRow(positioned);
    };
    recomputeEventOverlayPositionsRef.current = recomputeEventOverlayPositions;

    // Handled manually rather than via lightweight-charts' own
    // `autoSize: true` — this cockpit mounts the chart inside a pane that
    // can be `display:none` (the mobile/iPad "Chart" tab starts hidden;
    // the desktop 3-column pane can also cross the xl breakpoint on
    // resize). autoSize correctly picks up the new pixel size on a
    // hidden->visible transition, but does NOT itself re-fit the visible
    // time range afterward, leaving all bars compressed into whatever
    // narrow bar-spacing was last fit — most of the panel renders blank
    // with a cramped sliver of candles at one edge. Doing resize() and
    // fitContent() together, in that order, inside one observer removes
    // the ordering race a second independent observer would risk.
    //
    // BOTH dimensions are read from the observed box, not just width: the
    // chart's parent chain now flexes the container to fill whatever
    // vertical space the primary cockpit's viewport-bounded row actually
    // has (App.tsx's `xl:h-[calc(100vh-150px)]` + flex-1 chart wrapper),
    // so a hard-coded height here would silently reintroduce the exact
    // dead-space bug this fixes — the chart would sit inside a
    // correctly-tall flex box while itself staying a fixed small size.
    const resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || rect.width <= 0) return;
      const height = Math.max(rect.height, MIN_CHART_HEIGHT);
      chart.resize(rect.width, height);
      suppressInteractionRef.current = true;
      chart.timeScale().fitContent();
      suppressInteractionRef.current = false;
      recomputeEventOverlayPositions();
    });
    resizeObserver.observe(containerRef.current);

    // Modifier-to-zoom (Google Maps/TradingView pattern), completing the
    // scroll-trap fix above: handleScale.mouseWheel is off, so the library
    // itself no longer reacts to wheel events at all — this listener is
    // the only thing that can zoom on wheel now, and only does so when
    // Ctrl (Win/Linux) or Cmd (Mac) is held. Anything else (plain wheel)
    // returns immediately without calling preventDefault, so the event
    // falls through to the browser's normal page-scroll handling exactly
    // as it would over any non-chart element. `{ passive: false }` is
    // required for preventDefault to have any effect on a wheel listener.
    // Zoom is centered on the cursor's own bar (via coordinateToLogical),
    // not the chart's midpoint, matching how the library's built-in wheel-
    // zoom used to behave before it was turned off above.
    const onWheel = (event: WheelEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      const timeScale = chart.timeScale();
      const range = timeScale.getVisibleLogicalRange();
      if (!range || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pointerLogical = timeScale.coordinateToLogical(event.clientX - rect.left);
      const pivot = pointerLogical ?? (range.from + range.to) / 2;
      // deltaY > 0 (scroll down / pinch out on a trackpad) zooms out;
      // deltaY < 0 zooms in. exp() gives a smooth, symmetric factor around
      // 1 regardless of the OS/device's wheel-delta scale.
      const factor = Math.exp(event.deltaY * 0.001);
      // suppressInteractionRef is left at its default (false) here — this
      // is a real operator gesture, so the setVisibleLogicalRange call
      // below is meant to reach the subscribeVisibleLogicalRangeChange
      // handler's onUserInteraction call, same as any pan/zoom.
      timeScale.setVisibleLogicalRange({
        from: pivot - (pivot - range.from) * factor,
        to: pivot + (range.to - pivot) * factor,
      });
    };
    containerRef.current.addEventListener("wheel", onWheel, { passive: false });

    // Two jobs on the same event, both from the owner-approved fixes to
    // this panel:
    //  1. Fix "blank history when panning left" — once the visible range
    //     nears the left edge of what's currently loaded, fetch further
    //     back (doubling lookback_days, capped at the backend's 500-day
    //     max) and prepend it, without ever calling fitContent() as part
    //     of this path (see fetchMoreHistory below — that would yank the
    //     operator's current view, reintroducing the original zoom-reset
    //     bug this same file fixed earlier today).
    //  2. Fix "auto-follow must not hijack the chart while engaged" — a
    //     real pan/zoom is exactly the signal App.tsx needs to back off
    //     auto-follow. suppressInteractionRef is set around every
    //     programmatic range change in this file (initial fit, resize fit,
    //     symbol/timeframe-switch fit, fetch-more's view restore) so only
    //     an actual operator gesture reaches onUserInteraction.
    //  3. Bottom range-slider sync — every range change (pan, zoom, or a
    //     drag on the slider itself, since that also goes through
    //     setVisibleLogicalRange) updates visibleRange so the slider's
    //     window redraws in the same place the chart just moved to.
    chart.timeScale().subscribeVisibleLogicalRangeChange((range: LogicalRange | null) => {
      if (!suppressInteractionRef.current) onUserInteractionRef.current?.();
      setVisibleRangeState(range);
      recomputeEventOverlayPositions();
      if (!range) return;
      if (fetchingMoreRef.current) return;
      if (historyExhaustedRef.current) return;
      if (!symbolRef.current || !barsRef.current.length) return;
      if (range.from > FETCH_MORE_EDGE_BARS) return;
      fetchMoreHistory();
    });

    return () => {
      resizeObserver.disconnect();
      containerRef.current?.removeEventListener("wheel", onWheel);
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Fetch-more-on-pan: re-fetch the same symbol/timeframe with a wider
  // lookback_days and merge the result in. Deliberately a full re-fetch,
  // not incremental pagination — the backend endpoint already supports
  // lookback_days up to 500 with no pagination story, so doubling it and
  // re-requesting is the simple option the fix calls for.
  async function fetchMoreHistory() {
    const sym = symbolRef.current;
    const tf = timeframeRef.current;
    const chart = chartRef.current;
    if (!sym || !chart) return;
    fetchingMoreRef.current = true;
    const newLookback = lookbackDaysRef.current * 2;
    try {
      const resp = await api.prices(sym, newLookback, tf);
      // The operator may have switched symbol/timeframe while this was in
      // flight — a stale response must never clobber the panel now showing
      // something else.
      if (symbolRef.current !== sym || timeframeRef.current !== tf) return;
      if (resp.error || !resp.bars.length) return;
      const addedCount = resp.bars.length - barsRef.current.length;
      lookbackDaysRef.current = newLookback;
      if (addedCount <= 0) {
        // Already had everything this wider window offers — the real data
        // floor, not an arbitrary ceiling. Stop asking; see
        // historyExhaustedRef above.
        historyExhaustedRef.current = true;
        return;
      }
      const range = chart.timeScale().getVisibleLogicalRange();
      if (range) pendingRangeRestoreRef.current = { range, addedCount };
      setBars(resp.bars);
      const last = resp.bars[resp.bars.length - 1];
      setLastBarTime(last.timestamp || last.date);
    } catch {
      // Silent: a failed fetch-more just leaves the operator at the edge
      // they already panned to; the next pan attempt retries.
    } finally {
      fetchingMoreRef.current = false;
    }
  }

  // Bottom range-slider drag: converts horizontal mouse movement on the
  // slider's window handle into a logical-range shift on the chart, via
  // the same timeScale().setVisibleLogicalRange the fetch-more view-restore
  // above already uses. Kept as plain mousedown/mousemove/mouseup (no
  // library) per the fix's own scope — a drag needs a document-level
  // listener (not just on the handle) so the drag keeps tracking even if
  // the pointer leaves the small handle element mid-drag.
  //
  // Worked example: a 600px-wide track, 300 total bars loaded, window
  // currently showing the most recent 50 bars (logical range from=250,
  // to=300 — window left = 250/300*600 = 500px, width = 50/300*600 =
  // 100px). Dragging left by 120px: deltaBars = -120px / 600px * 300 bars
  // = -60 bars, so newFrom = 250-60 = 190, newTo = 300-60 = 240 — the
  // window (and the chart's visible range) shifts 60 bars earlier, still
  // 50 bars wide. Clamped so newFrom never goes below 0 and newTo never
  // exceeds totalBars, sliding the window's edges together rather than
  // shrinking it once it hits either end of the loaded data.
  function onSliderWindowMouseDown(e: ReactMouseEvent) {
    const track = sliderTrackRef.current;
    const chart = chartRef.current;
    const range = visibleRange;
    const totalBars = barsRef.current.length;
    if (!track || !chart || !range || totalBars < 1) return;
    e.preventDefault();
    onUserInteractionRef.current?.();
    sliderDragRef.current = {
      startX: e.clientX,
      startFrom: range.from,
      startTo: range.to,
      trackWidth: track.getBoundingClientRect().width,
    };
    const handleMove = (moveEvent: MouseEvent) => {
      const drag = sliderDragRef.current;
      if (!drag || drag.trackWidth <= 0) return;
      const deltaBars = ((moveEvent.clientX - drag.startX) / drag.trackWidth) * totalBars;
      const windowWidth = drag.startTo - drag.startFrom;
      let newFrom = drag.startFrom + deltaBars;
      let newTo = drag.startTo + deltaBars;
      if (newFrom < 0) {
        newFrom = 0;
        newTo = windowWidth;
      } else if (newTo > totalBars) {
        newTo = totalBars;
        newFrom = totalBars - windowWidth;
      }
      chart.timeScale().setVisibleLogicalRange({ from: newFrom, to: newTo });
    };
    const handleUp = () => {
      sliderDragRef.current = null;
      document.removeEventListener("mousemove", handleMove);
      document.removeEventListener("mouseup", handleUp);
    };
    document.addEventListener("mousemove", handleMove);
    document.addEventListener("mouseup", handleUp);
  }

  function clearChart() {
    candleSeriesRef.current?.setData([]);
    volumeSeriesRef.current?.setData([]);
    setBars([]);
  }

  useEffect(() => {
    // Bug fix (cockpit pass 3, item 1): lightweight-charts permanently
    // disables the price scale's autoScale the moment the operator drags
    // the price axis by hand (its own built-in "pin my manual zoom"
    // behavior) — there is no code path in this file that ever did that,
    // it's the library's default reaction to that one gesture. Once
    // disabled, EVERY future setData() call (a new symbol, a new
    // timeframe) keeps rendering against that stale manually-set range
    // instead of fitting the new data, which is exactly the reported
    // symptom: click MSFT (~$491) then CMCSA (~$26.78) and the axis stays
    // parked in the old range with the new candles invisible, and no
    // amount of dragging recovers it because the SAME gesture that would
    // "fix" it is what broke it. Re-asserting autoScale here — scoped to
    // this effect's [symbol, timeframe] deps, not the render effect below
    // that also fires on every 20s quote poll — means a symbol/timeframe
    // switch always starts from a clean fit, while a manual zoom the
    // operator makes mid-session on the SAME symbol/timeframe survives
    // until the next real switch instead of being wiped by the next poll.
    candleSeriesRef.current?.priceScale().applyOptions({ autoScale: true });
    // Bug fix: fitContent() must NOT run here, against whatever's still in
    // the series from the previous symbol/timeframe — lightweight-charts
    // won't auto-fit the new bars once a range has been explicitly set, so
    // fitting now just locks in a stale window the real data then silently
    // renders into. Deferred via needsInitialFitRef to the render effect,
    // right after setData() actually applies the new bars.
    needsInitialFitRef.current = true;
    // A real symbol/timeframe switch is a fresh chart load: fetch-more-on-
    // pan's "how far back have we already gone" state and any pending
    // range restore from the previous symbol/timeframe no longer apply.
    fetchingMoreRef.current = false;
    historyExhaustedRef.current = false;
    pendingRangeRestoreRef.current = null;
    if (!symbol) {
      setBarCount(0);
      clearChart();
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setBars([]);
    setBarCount(0);
    setLastBarTime(null);
    const selectedTimeframe = TIMEFRAMES.find((item) => item.value === timeframe)!;
    lookbackDaysRef.current = selectedTimeframe.lookbackDays;
    api
      .prices(symbol, selectedTimeframe.lookbackDays, timeframe)
      .then((resp) => {
        if (cancelled) return;
        if (resp.error) {
          setError(resp.error);
          setBarCount(0);
          clearChart();
          return;
        }
        if (!resp.bars.length) {
          setBarCount(0);
          setLastBarTime(null);
          clearChart();
          return;
        }
        setBars(resp.bars);
        const last = resp.bars[resp.bars.length - 1];
        setLastBarTime(last.timestamp || last.date);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
          setLastBarTime(null);
          clearChart();
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, timeframe]);

  useEffect(() => {
    setQuote(null);
    setQuoteError(null);
    setQuoteAsOf(null);
  }, [symbol]);

  // Dividend/earnings markers — one-shot fetch per symbol (not polled: see
  // the state declaration above). A stale in-flight response from a symbol
  // the operator has since clicked away from must never land on the wrong
  // chart, same guard fetchMoreHistory uses.
  useEffect(() => {
    setDividendEvents([]);
    setEarningsEvents([]);
    if (!symbol) return;
    let cancelled = false;
    api
      .events(symbol)
      .then((resp) => {
        if (cancelled || resp.error) return;
        setDividendEvents(resp.dividends);
        setEarningsEvents(resp.earnings);
      })
      .catch(() => {
        // Best-effort overlay — a failed fetch just means no markers for
        // this symbol, never a chart-wide error.
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  // Genuinely live quote for the charted symbol — GET /quotes, wrapping the
  // same read-only Alpaca snapshot the accepted intraday scanner uses.
  // Deliberately never derived from the candlestick bars above: a daily bar
  // is historical (possibly up to one session behind during market hours),
  // this is Mission Control's actual current-price source. Polled on the
  // same cadence as the rest of the cockpit; a failed poll keeps the last
  // known quote on screen tagged with its own error, never silently blank.
  usePoll(() => {
    if (!symbol) {
      setQuote(null);
      setQuoteError(null);
      setQuoteAsOf(null);
      return;
    }
    api
      .quotes([symbol])
      .then((resp) => {
        const q = resp.quotes.find((x) => x.symbol === symbol) ?? null;
        if (resp.error) {
          setQuoteError(resp.error);
        } else {
          setQuote(q);
          setQuoteError(null);
          const asOf = new Date(resp.as_of);
          setQuoteAsOf(Number.isNaN(asOf.getTime()) ? null : asOf);
        }
      })
      .catch((err) => setQuoteError(err.message));
  }, [symbol]);

  // Bug fix (2026-09-10, "frozen intraday bars"): the load effect above
  // ([symbol, timeframe]) is a genuine one-shot fetch — it never re-runs on
  // its own, so without this the candles it loaded would sit there
  // unchanged for as long as the panel stays open, even though the quote
  // poll right above keeps advancing and even though the backend's own
  // caching (broker.get_intraday_chart_bars) always serves today's portion
  // fresh on every call. The backend was never the problem; nothing on the
  // frontend was ever asking it again. Same cadence as the quote poll,
  // reuses whatever lookbackDaysRef currently holds (fetch-more-on-pan may
  // have already widened it, so this never re-narrows the loaded window),
  // and is a silent best-effort refresh — never touches `loading`/`error`
  // or the fit/zoom refs, so it can't fight the operator's own pan/zoom or
  // the initial-load/fetch-more machinery above. Skips a tick outright
  // while either of those is already in flight for this symbol.
  usePoll(() => {
    const sym = symbolRef.current;
    const tf = timeframeRef.current;
    if (!sym || fetchingMoreRef.current || loadingRef.current) return;
    api
      .prices(sym, lookbackDaysRef.current, tf)
      .then((resp) => {
        // Same stale-response guard as fetch-more-on-pan: the operator may
        // have switched symbol/timeframe while this poll tick was in
        // flight.
        if (symbolRef.current !== sym || timeframeRef.current !== tf) return;
        if (resp.error || !resp.bars.length) return;
        setBars(resp.bars);
        const last = resp.bars[resp.bars.length - 1];
        setLastBarTime(last.timestamp || last.date);
      })
      .catch(() => {
        // Silent: matches fetch-more-on-pan's posture — the next poll
        // tick retries, the quote poll's own error state already tells
        // the operator when data is failing to refresh.
      });
  }, [symbol, timeframe]);

  // Plot truth: completed historical candles, an optional forming candle
  // from current-session OHLC, and independently labeled live/previous-
  // close lines. This prevents yesterday's final candle from visually
  // masquerading as today's current price when the two diverge.
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!candleSeries || !volumeSeries) return;
    const colors = readThemeColors();
    const candles = chartCandles(bars, quote, etDateKey(new Date()), timeframe);
    candleSeries.setData(candles);
    volumeSeries.setData(toVolume(bars, timeframe, colors));
    chartRef.current?.applyOptions({
      timeScale: { timeVisible: timeframe !== "1d", secondsVisible: false },
    });
    setBarCount(candles.length);

    // Fetch-more-on-pan just prepended older bars ahead of this setData —
    // deliberately NOT followed by fitContent() (that would yank the
    // operator's current view, the exact bug the earlier zoom-reset fix
    // addressed). Instead, shift the previously-visible logical range
    // right by however many bars were prepended, landing the operator
    // exactly where they were panned to. Consumed here (not in
    // fetchMoreHistory itself) so it applies in the same synchronous pass
    // that actually prepends the data to the series.
    if (needsInitialFitRef.current) {
      // A genuine symbol/timeframe switch: fit now that the real new bars
      // are actually in the series (see needsInitialFitRef's declaration
      // for why this can't happen earlier). Takes priority over a pending
      // fetch-more range restore — a fresh switch has no "operator's pan
      // position" to preserve, and clears any that carried over.
      needsInitialFitRef.current = false;
      pendingRangeRestoreRef.current = null;
      suppressInteractionRef.current = true;
      chartRef.current?.timeScale().fitContent();
      suppressInteractionRef.current = false;
    } else if (pendingRangeRestoreRef.current) {
      const { range, addedCount } = pendingRangeRestoreRef.current;
      pendingRangeRestoreRef.current = null;
      suppressInteractionRef.current = true;
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: range.from + addedCount,
        to: range.to + addedCount,
      });
      suppressInteractionRef.current = false;
    }

    if (livePriceLineRef.current) candleSeries.removePriceLine(livePriceLineRef.current);
    if (entryLineRef.current) candleSeries.removePriceLine(entryLineRef.current);
    if (stopLineRef.current) candleSeries.removePriceLine(stopLineRef.current);
    livePriceLineRef.current = null;
    entryLineRef.current = null;
    stopLineRef.current = null;

    // Owner correction: the LIVE line is created FIRST, on purpose — every
    // price line added after it (ENTRY, STOP) stacks visually on top of
    // it, so it never obscures the trader's own reference lines. It's
    // also thin, dashed and muted (the same dim ink as axis text, never
    // the bright accent, and never green/red — those are reserved for
    // P&L) rather than a bold solid line — it needs to be readable, not
    // dominant ("in my face"). The label still renders pinned to the
    // price axis (axisLabelVisible), never floating over the chart body.
    // PREV CLOSE was removed entirely (every timeframe) per the same
    // correction — the owner called it "not relevant."
    if (quote?.last_price != null) {
      livePriceLineRef.current = candleSeries.createPriceLine({
        price: quote.last_price,
        color: colors.textMuted,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "LIVE",
      });
    }
    const entry = entryPriceLine(symbol, positions, { green: colors.green, red: colors.red });
    if (entry) {
      entryLineRef.current = candleSeries.createPriceLine({
        price: entry.price,
        color: entry.color,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: entry.title,
      });
    }
    const stop = positionStopLine(symbol, positions, openOrders, stopLookupTrades, { amber: colors.amber });
    if (stop) {
      stopLineRef.current = candleSeries.createPriceLine({
        price: stop.price,
        color: stop.color,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: stop.title,
      });
    }
  }, [bars, quote, timeframe, symbol, positions, openOrders, stopLookupTrades]);

  // Real BUY/SELL execution markers on the price series — the vision
  // board's chart mockup shows these; `lightweight-charts` already
  // supports them natively (`ISeriesApi.setMarkers`), so this is wiring an
  // existing capability, not a new visualization primitive. Re-applied
  // whenever the bar data (barCount) or the trade list changes, so a stale
  // marker set from a previously-charted symbol never lingers.
  //
  // The blue entry-TIME markers (entryTimeMarkers) are merged in here
  // rather than given a separate setMarkers call — lightweight-charts v4's
  // setMarkers replaces the whole marker set per series, so a second call
  // would just clobber the first. Sourced from stopLookupTrades, not the
  // session-scoped `trades` prop above, for the same reason
  // positionStopLine below does: a held position may have been entered in
  // an earlier session than whichever one is currently selected.
  //
  // Dividend/earnings are deliberately NOT in this array any more — see
  // dividendOverlayItems/earningsOverlayItems above. They render through
  // the separate fixed-row overlay below instead of as price-anchored
  // SeriesMarkers.
  useEffect(() => {
    if (!candleSeriesRef.current) return;
    if (!symbol || barCount === 0) {
      candleSeriesRef.current.setMarkers([]);
      return;
    }
    const colors = readThemeColors();
    const availableTimes = chartCandles(
      bars, quote, etDateKey(new Date()), timeframe
    ).map((candle) => candle.time);
    const markers = [
      ...tradeMarkers(
        symbol, trades, { green: colors.green, red: colors.red },
        timeframe, availableTimes
      ),
      ...entryTimeMarkers(
        symbol, positions, stopLookupTrades, colors.accent, timeframe, availableTimes
      ),
    ].sort((a, b) => String(a.time).localeCompare(String(b.time)));
    candleSeriesRef.current.setMarkers(markers);
  }, [symbol, trades, barCount, bars, quote, timeframe, positions, stopLookupTrades]);

  // Fixed bottom row for dividend/earnings — exactly TradingView's real
  // behavior (owner re-test, 2026-09-10): a thin strip of small icons at a
  // CONSTANT Y position just above the time axis, completely decoupled
  // from price, instead of the previous belowBar SeriesMarker approach
  // (which anchors to that candle's low, not a screen row — see the note
  // on dividendOverlayItems above for why that was still wrong).
  //
  // Two-step: `eventOverlayItems` (time + kind + color, no pixels yet) is
  // pure data recomputed only when the underlying events/bars/timeframe
  // actually change; `eventOverlayRow` (time converted to an X pixel via
  // timeScale().timeToCoordinate()) is recomputed on every visible-range
  // change too (pan/zoom/slider drag), via the SAME
  // subscribeVisibleLogicalRangeChange subscription the fetch-more-on-pan
  // logic above already uses, plus the resize observer (width changes
  // shift every X coordinate) — see recomputeEventOverlayPositionsRef,
  // wired into both inside the mount-once chart effect.
  const [eventOverlayItems, setEventOverlayItems] = useState<EventOverlayItem[]>([]);
  const [eventOverlayRow, setEventOverlayRow] = useState<
    { x: number; kind: "dividend" | "earnings"; color: string; title: string }[]
  >([]);
  const eventOverlayItemsRef = useRef<EventOverlayItem[]>([]);
  // Which dividend/earnings marker (if any) is currently hovered — drives
  // the custom tooltip below. Keyed by the marker's own x pixel (unique
  // within one render of eventOverlayRow) rather than an index, so a
  // recompute mid-hover (pan/zoom/resize) that reshuffles array order
  // still matches the same marker instead of showing the wrong tooltip.
  const [hoveredEvent, setHoveredEvent] = useState<{ x: number; title: string } | null>(null);
  // Set once, inside the mount-once chart effect below, to the actual
  // recompute function — a ref (not a direct call) because that effect
  // runs once at mount, before this state/the function below even exist
  // on the first pass, and must always invoke whatever the LATEST
  // recompute closure is (it closes over chartRef/containerRef, which
  // don't change, so this is just avoiding a re-subscribe dance).
  const recomputeEventOverlayPositionsRef = useRef<() => void>(() => {});

  useEffect(() => {
    if (!symbol || barCount === 0) {
      setEventOverlayItems([]);
      return;
    }
    const colors = readThemeColors();
    const availableTimes = chartCandles(
      bars, quote, etDateKey(new Date()), timeframe
    ).map((candle) => candle.time);
    const items = [
      ...dividendOverlayItems(dividendEvents, colors.agent, timeframe, availableTimes),
      ...earningsOverlayItems(
        earningsEvents, { past: colors.agent, upcoming: colors.agentFaded },
        timeframe, availableTimes
      ),
    ];
    setEventOverlayItems(items);
  }, [symbol, barCount, bars, quote, timeframe, dividendEvents, earningsEvents]);

  useEffect(() => {
    eventOverlayItemsRef.current = eventOverlayItems;
    recomputeEventOverlayPositionsRef.current();
  }, [eventOverlayItems]);

  // "degraded", not "ok" — a symbol is selected but no real bars rendered
  // (e.g. no Alpaca market-data credentials in this environment). An "OK"
  // pill over a blank chart would misrepresent a known data gap as
  // everything-fine.
  const status = error
    ? "error"
    : loading
    ? "loading"
    : quoteError
    ? quote
      ? "stale"
      : "degraded"
    : symbol && barCount === 0
    ? "degraded"
    : "ok";
  const overlay = !symbol
    ? { heading: "No symbol charted", detail: "Click a candidate to chart it." }
    : error
    ? { heading: `${symbol} price history unavailable`, detail: error }
    : !loading && barCount === 0
    ? { heading: `No ${timeframe} bars for ${symbol}`, detail: "Market-data provider returned no bars for this symbol/range." }
    : null;

  // The candlesticks are historical bars (up to one session behind during
  // market hours — Alpaca's "today" daily bar isn't complete yet); this
  // line is the one place on this panel that claims to be current, sourced
  // and timestamped separately (GET /quotes) so it's never confused with —
  // or silently mismatched against — the chart itself. Never fabricated:
  // absent/errored quote data says so instead of going blank.
  const barsRunBehindToday = Boolean(
    timeframe === "1d" && symbol && lastBarTime && lastBarTime !== etDateKey(new Date())
  );
  const hasFormingCandle = timeframe === "1d" && chartCandles([], quote).length === 1;
  const intradayThrough = timeframe !== "1d" && lastBarTime
    ? formatEasternTime(
        new Date(lastBarTime.endsWith("Z") || lastBarTime.includes("+") ? lastBarTime : `${lastBarTime}Z`),
        { hour: "2-digit", minute: "2-digit" }
      )
    : null;
  // The operator's own position in the charted symbol, echoed in the panel
  // subtitle as well as on the chart's entry line — the line label is
  // small and sits against the price scale; this is the readable version.
  // Bottom range-slider geometry — the full loaded series (0..bars.length)
  // is the track's domain; the window is the chart's own visible logical
  // range clamped into that domain, expressed as percentages so the CSS
  // does the pixel math. Clamped (not just cast) because
  // getVisibleLogicalRange() legitimately reports slightly outside
  // [0, bars.length) — lightweight-charts pads a small margin on either
  // side of the actual data — which would otherwise draw the window handle
  // spilling past the track.
  const totalBars = bars.length;
  const sliderWindow =
    totalBars > 0 && visibleRange
      ? (() => {
          const clamp = (v: number) => Math.min(totalBars, Math.max(0, v));
          const from = clamp(visibleRange.from);
          const to = clamp(visibleRange.to);
          const leftPct = (from / totalBars) * 100;
          // A floor on width keeps the handle grabbable even when the
          // operator is zoomed into a handful of bars out of a multi-year
          // series, where the true proportional width would be under a
          // pixel.
          const widthPct = Math.max(((to - from) / totalBars) * 100, 2);
          return { leftPct, widthPct: Math.min(widthPct, 100 - leftPct) };
        })()
      : null;
  const heldPosition = symbol ? positions.find((item) => item.symbol === symbol) : undefined;
  const positionLine = heldPosition
    ? ` · position ${fmtNum(heldPosition.qty)} @ ${fmtMoney(heldPosition.avg_entry)} · ${
        (heldPosition.unrealized_pnl ?? 0) >= 0 ? "+" : ""
      }${fmtMoney(heldPosition.unrealized_pnl)} unrealized`
    : "";
  const quoteLine = !symbol
    ? undefined
    : quote?.last_price != null
    ? `${quoteError ? "Last live" : "Live"} ${fmtMoney(quote.last_price)}${
        quoteAsOf ? ` · as of ${formatEasternTime(quoteAsOf, { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : ""
      }${quoteError ? ` · stale (refresh failed: ${quoteError})` : ""}${
        hasFormingCandle ? " · today’s forming candle" : " · live price line"
      }${barsRunBehindToday ? ` · completed history through ${lastBarTime}` : ""}${
        intradayThrough ? ` · ${timeframe} bars through ${intradayThrough}` : ""
      }`
    : quoteError
    ? `Live quote unavailable (${quoteError})`
    : barsRunBehindToday
    ? `Chart history through ${lastBarTime} — no live quote loaded yet`
    : undefined;

  return (
    <Panel
      title={symbol ? `Price — ${symbol}` : "Price chart"}
      status={status}
      subtitle={quoteLine ? `${quoteLine}${positionLine}` : positionLine || undefined}
      actions={
        <div className="flex items-center gap-1" aria-label="Chart timeframe">
          {TIMEFRAMES.map((item) => (
            <Button
              key={item.value}
              type="button"
              size="xs"
              color="cyan"
              variant={timeframe === item.value ? "primary" : "secondary"}
              onClick={() => {
                onUserInteractionRef.current?.();
                setTimeframe(item.value);
              }}
            >
              {item.label}
            </Button>
          ))}
          <Button
            type="button"
            size="xs"
            color="cyan"
            variant="secondary"
            onClick={() => {
              onUserInteractionRef.current?.();
              chartRef.current?.timeScale().fitContent();
            }}
          >
            Reset zoom
          </Button>
        </div>
      }
      full
    >
      {/* Always mounted at a real size, never display:none — the chart
          object is created once against this container at mount time and
          the manual ResizeObserver above needs a real box to measure from
          the start. `h-full` lets it inherit whatever height App.tsx's
          flex-1 chart wrapper actually computed (viewport-bounded on
          desktop). Below `xl` (the mobile/iPad single-pane view, which has
          no viewport-bounded ancestor to inherit from) the chart was
          previously pinned to a flat 320px regardless of the device's
          actual screen — cramped on every iPad size, reported by the
          operator. `60vh` scales with the real viewport instead (an iPad
          in portrait gets meaningfully more chart than landscape, both get
          far more than the old constant); `min-h-[280px]` is still the
          same floor as MIN_CHART_HEIGHT so the container and the chart's
          own resize logic can never disagree. The overlay below sits on
          top of that same grid rather than adding a second block of
          vertical space beneath it, so an empty/degraded state reads as a
          designed placeholder instead of prime chart space going to waste
          on a blank "OK" box. */}
      {/* Fix (owner UI pass): candle/volume area + the bottom range slider
          now live in ONE flex column that is itself sized exactly the way
          the old lone div was (`h-[60vh]` below `xl` where there's no
          viewport-bounded ancestor to inherit real height from — see the
          comment on the outer container above; `h-full` within `xl`, where
          the dockview panel/App.tsx flex-1 wrapper DOES provide one). The
          bug this fixes: previously the chart sub-div claimed the FULL
          h-[60vh]/h-full box on its own, and the slider was appended as a
          plain sibling AFTER it — so together they were always taller than
          the box, and `.panel-body`'s `overflow-x-auto` (which the CSS spec
          forces `overflow-y` to computed `auto` alongside, not `visible`,
          once one axis is scrolling) surfaced that overflow as an ugly
          default browser scrollbar just to reach the slider. Making this a
          flex column with the chart area `flex-1 min-h-0` and the slider
          `flex-shrink-0` instead means the two always sum to exactly the
          box height — the chart genuinely shrinks to leave room for the
          slider rather than both trying to be 100% at once. No new height
          math needed: the existing ResizeObserver below already observes
          containerRef, which is `w-full h-full` of this now-correctly-sized
          flex-1 box, so it keeps picking up the right pixel size on every
          resize (dockview panel resize, browser resize, or symbol switch)
          with no separate JS-computed height and no overflow/scroll relied
          on as the mechanism. MIN_CHART_HEIGHT (280px) remains the real
          floor, enforced where it always was — the resize observer's own
          `Math.max(rect.height, MIN_CHART_HEIGHT)` — so the chart can never
          be squeezed to unusable regardless of how the flex split lands. */}
      <div className="flex h-[60vh] xl:h-full min-h-[280px] flex-col">
        <div className="relative flex-1 min-h-0">
          <div ref={containerRef} className="w-full h-full" />
          {/* Dividend/earnings fixed bottom row (owner re-test, 2026-09-10):
              a thin strip of icons at a CONSTANT screen Y, just above
              lightweight-charts' own time-axis labels and below the volume
              pane — TradingView's real corporate-action row, decoupled from
              price. `bottom`/`height` below are fixed pixel offsets tuned to
              this chart's own default time-axis height (~24px at fontSize
              11, set in the mount effect above), not derived from the
              chart's actual pixel geometry — lightweight-charts exposes no
              API for "height of the time axis," so this is the same kind of
              tuned-constant every other pixel-offset in this file already
              is. `pointer-events-none` on the row itself so it never blocks
              chart panning; each icon re-enables pointer events only for
              itself. Each icon's `x` is a chart-canvas-relative pixel from
              timeScale().timeToCoordinate(), recomputed on every pan/zoom/
              resize (see recomputeEventOverlayPositions above) — never tied
              to any candle's price.

              Fix (owner re-test, 2026-09-10, round 2): the previous version
              of this row rendered each marker as a bare 8x8px dot with no
              letter and relied solely on the native `title` attribute for
              detail on hover — confirmed live as "barely-visible tiny dot,
              hovering does nothing" (title tooltips are the browser's own
              affordance; timing/visibility is not reliable, and at 8px the
              dot reads as noise, not a labeled marker). Matching
              TradingView's actual behavior (compared directly against it):
              each marker is now a fixed MARKER_SIZE (16px) shape — circle
              for dividend, square for earnings — with the "D"/"E" letter
              drawn directly inside it in a contrasting color, legible with
              zero interaction. Hover/click is now a real custom tooltip
              (plain conditional-render div, positioned near the marker,
              shown on onMouseEnter/hidden on onMouseLeave — the same
              plain-React-state approach already used elsewhere in this
              file, no tooltip library) rather than the native `title`,
              and only supplies supplementary detail (amount/date), never
              the marker's identity. `pointer-events-auto` is set
              explicitly on each marker (the row itself stays
              `pointer-events-none` so it never blocks chart panning
              between markers) so hover actually reaches the element.

              Fix (owner re-test, round 3, 2026-09-10) — "buried a little
              bit below the [LIVE] line", hover/click still doing nothing:
              traced the DOM/paint order here against lightweight-charts'
              own internal structure (its canvases are appended as
              descendants of `containerRef` above, itself an unpositioned
              `w-full h-full` div with no z-index anywhere in the library's
              own source — the only `z-index` it ever sets is a scoped
              `a#tv-attr-logo` id selector, unrelated). Per CSS stacking
              rules a positioned sibling (this row, `position: absolute`)
              already paints above an earlier UNpositioned sibling
              (`containerRef`) regardless of z-index or DOM depth, so this
              row should already have been receiving the pointer events —
              tracing did not reproduce a genuine z-index defect. Bumped
              `bottom` 20 -> 26 anyway for real vertical clearance from the
              LIVE price line drawn on the canvas at low prices (a canvas
              paint, not a DOM element — can't itself steal events, but the
              two do visually collide at some prices) and added an explicit
              `zIndex` here and on the tooltip below as cheap, harmless
              insurance against any stacking context the library's DOM
              structure might otherwise introduce. If hover/click still
              doesn't respond after this, the actual defect is elsewhere
              (most likely eventOverlayRow's data/x-coordinates, or the
              hoveredEvent state update itself) and needs a live devtools
              trace, not another stacking tweak. */}
          {eventOverlayRow.length > 0 && (
            <div
              className="absolute left-0 right-0 pointer-events-none z-20"
              style={{ bottom: 26, height: MARKER_SIZE }}
              aria-hidden="true"
            >
              {eventOverlayRow.map((item, i) => (
                <div
                  key={`${item.kind}-${i}-${item.x}`}
                  className="absolute pointer-events-auto flex items-center justify-center font-bold leading-none text-white cursor-default"
                  style={{
                    left: item.x - MARKER_SIZE / 2,
                    top: 0,
                    width: MARKER_SIZE,
                    height: MARKER_SIZE,
                    fontSize: 11,
                    backgroundColor: item.color,
                    borderRadius: item.kind === "dividend" ? "9999px" : "3px",
                    zIndex: 21,
                  }}
                  onMouseEnter={() => setHoveredEvent({ x: item.x, title: item.title })}
                  onMouseLeave={() => setHoveredEvent((current) => (current?.x === item.x ? null : current))}
                >
                  {item.kind === "dividend" ? "D" : "E"}
                </div>
              ))}
            </div>
          )}
          {/* Custom tooltip for the marker currently hovered above — plain
              conditional render + inline positioning, matching how this
              file already builds every other overlay (no tooltip library).
              Anchored just above the marker row so it never covers the
              marker itself; horizontally centered on the marker's own x,
              clamped so it can't run off either edge of the chart. */}
          {hoveredEvent && (
            <div
              className="absolute z-30 pointer-events-none rounded-md border border-border bg-panel px-2 py-1 text-[0.75rem] font-semibold text-ink whitespace-nowrap shadow-lg"
              style={{
                left: Math.max(4, hoveredEvent.x - 60),
                bottom: 26 + MARKER_SIZE + 6,
              }}
            >
              {hoveredEvent.title}
            </div>
          )}
          {overlay && (
            <div className="absolute inset-0 flex items-center justify-center pointer-events-none px-4">
              <div
                className={`text-center px-5 py-4 rounded-xl border max-w-[34ch] ${
                  error ? "border-neg/40 bg-panel/95" : "border-border bg-panel/95"
                }`}
              >
                <div className={`text-[0.95rem] font-bold ${error ? "text-neg" : "text-ink"}`}>{overlay.heading}</div>
                <div className="text-[0.8125rem] text-dim mt-1 leading-snug">{overlay.detail}</div>
              </div>
            </div>
          )}
        </div>
        {/* TradingView-style bottom time-navigation bar — the full loaded
            series as a thin track, with a draggable window over the chart's
            current visible range. Every timeframe uses this same widget
            against the same timeScale() visible-range API panning/zooming
            already drive, so there's nothing timeframe-specific here. Hidden
            until bars are actually loaded (sliderWindow is null before
            then) rather than rendering an empty/degenerate track. Restyled
            (owner UI pass) to match the thick rounded "pill" look of the
            native horizontal scrollbars on the Positions/Orders panels
            below — see the .chart-range-slider rule in styles/index.css;
            there was no existing custom scrollbar CSS class to reuse
            (those panels use unstyled native overflow-x-auto scrollbars,
            not a custom class), so this reproduces that same visual weight
            instead. Functional behavior (drag to pan, mirrors visible
            range, triggers fetch-more near the left edge via the
            subscribeVisibleLogicalRangeChange handler above) is unchanged. */}
        {sliderWindow && (
          <div
            ref={sliderTrackRef}
            className="chart-range-slider relative mt-2 flex-shrink-0 select-none"
            aria-label="Chart time range"
          >
            <div
              onMouseDown={onSliderWindowMouseDown}
              className="chart-range-slider-handle absolute top-1/2 -translate-y-1/2 cursor-grab active:cursor-grabbing"
              style={{ left: `${sliderWindow.leftPct}%`, width: `${sliderWindow.widthPct}%` }}
            />
          </div>
        )}
      </div>
    </Panel>
  );
}
