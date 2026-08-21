import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, CandlestickData, HistogramData, SeriesMarker, Time } from "lightweight-charts";
import { api, LiveQuote, PriceBar, TradeItem } from "../api/client";
import { Panel } from "./ui/Panel";
import { isExecutedTrade, etDateKey, fmtMoney } from "../lib/format";
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
  return {
    text: solid(text),
    border: solid(border),
    green: solid(green),
    red: solid(red),
    greenAlpha: alpha(green, 0.35),
    redAlpha: alpha(red, 0.35),
  };
}

function toCandles(bars: PriceBar[]): CandlestickData[] {
  return bars.map((b) => ({ time: b.date, open: b.open, high: b.high, low: b.low, close: b.close }));
}

function toVolume(bars: PriceBar[], colors: { greenAlpha: string; redAlpha: string }): HistogramData[] {
  return bars.map((b) => ({
    time: b.date,
    value: b.volume,
    color: b.close >= b.open ? colors.greenAlpha : colors.redAlpha,
  }));
}

// TradeItem.timestamp is a naive-UTC full datetime string
// ("YYYY-MM-DD HH:MM:SS"); the daily-bar chart's time axis only has a date
// component, so a marker's `time` must be truncated to match — otherwise
// lightweight-charts silently drops any marker whose exact timestamp isn't
// one of the series' existing data points.
function tradeMarkers(
  symbol: string,
  trades: TradeItem[],
  colors: { green: string; red: string }
): SeriesMarker<Time>[] {
  return trades
    .filter((t) => t.symbol === symbol && t.timestamp && isExecutedTrade(t) && (t.action === "BUY" || t.action === "SELL"))
    .map(
      (t): SeriesMarker<Time> => ({
        time: t.timestamp!.slice(0, 10) as Time,
        position: t.action === "BUY" ? "belowBar" : "aboveBar",
        color: t.action === "BUY" ? colors.green : colors.red,
        shape: t.action === "BUY" ? "arrowUp" : "arrowDown",
        text: `${t.action}${t.qty ? ` ${t.qty}` : ""}`,
      })
    )
    .sort((a, b) => (a.time as string).localeCompare(b.time as string));
}

// The chart must always resize to a real, non-trivial height even on a
// short/laptop viewport where `calc(100vh-150px)` leaves less room than a
// tall desktop monitor — never so short the candles become unreadable.
const MIN_CHART_HEIGHT = 240;

export function PriceChartPanel({ symbol, trades = [] }: { symbol: string | null; trades?: TradeItem[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [barCount, setBarCount] = useState(0);
  // Last historical bar's date — used only to tell the operator the chart
  // is running behind today (during market hours the daily bar for "today"
  // isn't a completed historical bar yet), never to infer a current price.
  const [lastBarDate, setLastBarDate] = useState<string | null>(null);
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
    api
      .prices(symbol, 120)
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
          setLastBarDate(null);
          clearChart();
          return;
        }
        const colors = readThemeColors();
        candleSeriesRef.current?.setData(toCandles(resp.bars));
        volumeSeriesRef.current?.setData(toVolume(resp.bars, colors));
        chartRef.current?.timeScale().fitContent();
        setBarCount(resp.bars.length);
        setLastBarDate(resp.bars[resp.bars.length - 1].date);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
          setLastBarDate(null);
          clearChart();
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
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
          setQuoteAsOf(new Date());
        }
      })
      .catch((err) => setQuoteError(err.message));
  }, [symbol]);

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
    candleSeriesRef.current.setMarkers(tradeMarkers(symbol, trades, { green: colors.green, red: colors.red }));
  }, [symbol, trades, barCount]);

  // "degraded", not "ok" — a symbol is selected but no real bars rendered
  // (e.g. no Alpaca market-data credentials in this environment). An "OK"
  // pill over a blank chart would misrepresent a known data gap as
  // everything-fine.
  const status = error ? "error" : loading ? "loading" : symbol && barCount === 0 ? "degraded" : "ok";
  const overlay = !symbol
    ? { heading: "No symbol charted", detail: "Click a candidate to chart it." }
    : error
    ? { heading: `${symbol} price history unavailable`, detail: error }
    : !loading && barCount === 0
    ? { heading: `No daily bars for ${symbol}`, detail: "Market-data provider returned no bars for this symbol/range." }
    : null;

  // The candlesticks are historical bars (up to one session behind during
  // market hours — Alpaca's "today" daily bar isn't complete yet); this
  // line is the one place on this panel that claims to be current, sourced
  // and timestamped separately (GET /quotes) so it's never confused with —
  // or silently mismatched against — the chart itself. Never fabricated:
  // absent/errored quote data says so instead of going blank.
  const barsRunBehindToday = Boolean(symbol && lastBarDate && lastBarDate !== etDateKey(new Date()));
  const quoteLine = !symbol
    ? undefined
    : quote?.last_price != null
    ? `Live ${fmtMoney(quote.last_price)}${quoteAsOf ? ` · as of ${quoteAsOf.toLocaleTimeString()}` : ""}${
        barsRunBehindToday ? ` · chart history through ${lastBarDate}` : ""
      }`
    : quoteError
    ? `Live quote unavailable (${quoteError})`
    : barsRunBehindToday
    ? `Chart history through ${lastBarDate} — no live quote loaded yet`
    : undefined;

  return (
    <Panel title={symbol ? `Price — ${symbol}` : "Price chart"} status={status} subtitle={quoteLine} full>
      {/* Always mounted at a real size, never display:none — the chart
          object is created once against this container at mount time and
          the manual ResizeObserver above needs a real box to measure from
          the start. `h-full` lets it inherit whatever height App.tsx's
          flex-1 chart wrapper actually computed (viewport-bounded on
          desktop, a fixed fallback below `xl` — see App.tsx); `min-h-[240px]`
          is the same floor as MIN_CHART_HEIGHT so the container and the
          chart's own resize logic can never disagree. The overlay below
          sits on top of that same grid rather than adding a second block
          of vertical space beneath it, so an empty/degraded state reads as
          a designed placeholder instead of prime chart space going to
          waste on a blank "OK" box. */}
      <div className="relative h-[320px] xl:h-full min-h-[240px]">
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
