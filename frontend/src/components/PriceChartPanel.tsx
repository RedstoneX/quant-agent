import { useEffect, useRef, useState } from "react";
import {
  createChart,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  CandlestickData,
  HistogramData,
  LineStyle,
  SeriesMarker,
  Time,
} from "lightweight-charts";
import { Button } from "@tremor/react";
import { api, ChartTimeframe, LiveQuote, PositionItem, PriceBar, TradeItem } from "../api/client";
import { Panel } from "./ui/Panel";
import { isExecutedTrade, etDateKey, fmtMoney, fmtNum } from "../lib/format";
import { usePoll } from "../lib/usePoll";

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
  return {
    text: solid(text),
    border: solid(border),
    green: solid(green),
    red: solid(red),
    accent: solid(accent),
    greenAlpha: alpha(green, 0.35),
    redAlpha: alpha(red, 0.35),
  };
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
// points (a 13:34:46 fill belongs on the 13:30 five-minute candle).
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

  const markerTime = (timestamp: string): Time | null => {
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
  };

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

/* Prev-close decision: kept on the intraday timeframes, dropped on 1D.
 * Intraday, "where did we close yesterday" is the reference the whole
 * session's move is measured against, so the line carries real
 * information. On the 120-day daily chart the previous close is simply the
 * second-to-last candle — already on screen, visually indistinguishable
 * from the last one — so the line is pure clutter across the full width of
 * the panel. Reported by the operator as noise on the daily view. */
export function shouldShowPrevClose(timeframe: ChartTimeframe): boolean {
  return timeframe !== "1d";
}

// The chart must always resize to a real, non-trivial height even on a
// short/laptop viewport where `calc(100vh-150px)` leaves less room than a
// tall desktop monitor — never so short the candles become unreadable.
// Matches the container's own `min-h-[280px]` floor below.
const MIN_CHART_HEIGHT = 280;
const TIMEFRAMES: Array<{ value: ChartTimeframe; label: string; lookbackDays: number }> = [
  { value: "5m", label: "5m Today", lookbackDays: 1 },
  { value: "15m", label: "15m", lookbackDays: 5 },
  { value: "1h", label: "1h", lookbackDays: 30 },
  { value: "1d", label: "1D", lookbackDays: 120 },
];

export function PriceChartPanel({
  symbol,
  trades = [],
  positions = [],
}: {
  symbol: string | null;
  trades?: TradeItem[];
  /* Live broker positions — used only to draw the operator's own average
   * entry for the charted symbol (see entryPriceLine above). Read-only,
   * like everything else on this surface. */
  positions?: PositionItem[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const livePriceLineRef = useRef<IPriceLine | null>(null);
  const previousCloseLineRef = useRef<IPriceLine | null>(null);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [barCount, setBarCount] = useState(0);
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("1d");
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

  useEffect(() => {
    if (!containerRef.current) return;
    const colors = readThemeColors();
    const initialWidth = containerRef.current.clientWidth || 600;
    const initialHeight = Math.max(containerRef.current.clientHeight, MIN_CHART_HEIGHT);
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "transparent" }, textColor: colors.text, fontSize: 11 },
      grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border },
      width: initialWidth,
      height: initialHeight,
    });
    const candleSeries = chart.addCandlestickSeries({
      upColor: colors.green,
      downColor: colors.red,
      borderVisible: false,
      wickUpColor: colors.green,
      wickDownColor: colors.red,
      // The series' default last-value line would label the final
      // historical close as if it were current. Only the explicitly
      // sourced LIVE (and, intraday, PREV CLOSE) lines below may make
      // that claim.
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
      chart.timeScale().fitContent();
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  function clearChart() {
    candleSeriesRef.current?.setData([]);
    volumeSeriesRef.current?.setData([]);
    setBars([]);
  }

  useEffect(() => {
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

    if (livePriceLineRef.current) candleSeries.removePriceLine(livePriceLineRef.current);
    if (previousCloseLineRef.current) candleSeries.removePriceLine(previousCloseLineRef.current);
    if (entryLineRef.current) candleSeries.removePriceLine(entryLineRef.current);
    livePriceLineRef.current = null;
    previousCloseLineRef.current = null;
    entryLineRef.current = null;

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
    if (shouldShowPrevClose(timeframe) && quote?.prev_close != null) {
      previousCloseLineRef.current = candleSeries.createPriceLine({
        price: quote.prev_close,
        color: colors.text,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "PREV CLOSE",
      });
    }
    if (quote?.last_price != null) {
      livePriceLineRef.current = candleSeries.createPriceLine({
        price: quote.last_price,
        color: colors.accent,
        lineWidth: 2,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: "LIVE",
      });
    }
    chartRef.current?.timeScale().fitContent();
  }, [bars, quote, timeframe, symbol, positions]);

  // Real BUY/SELL execution markers on the price series — the vision
  // board's chart mockup shows these; `lightweight-charts` already
  // supports them natively (`ISeriesApi.setMarkers`), so this is wiring an
  // existing capability, not a new visualization primitive. Re-applied
  // whenever the bar data (barCount) or the trade list changes, so a stale
  // marker set from a previously-charted symbol never lingers.
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
    candleSeriesRef.current.setMarkers(
      tradeMarkers(
        symbol, trades, { green: colors.green, red: colors.red },
        timeframe, availableTimes
      )
    );
  }, [symbol, trades, barCount, bars, quote, timeframe]);

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
    ? new Date(lastBarTime).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;
  // The operator's own position in the charted symbol, echoed in the panel
  // subtitle as well as on the chart's entry line — the line label is
  // small and sits against the price scale; this is the readable version.
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
        quoteAsOf ? ` · as of ${quoteAsOf.toLocaleTimeString()}` : ""
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
              onClick={() => setTimeframe(item.value)}
            >
              {item.label}
            </Button>
          ))}
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
      <div className="relative h-[60vh] xl:h-full min-h-[280px]">
        <div ref={containerRef} className="w-full h-full" />
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
    </Panel>
  );
}
