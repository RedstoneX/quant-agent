import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, CandlestickData, HistogramData, SeriesMarker, Time } from "lightweight-charts";
import { api, PriceBar, TradeItem } from "../api/client";
import { Panel } from "./ui/Panel";
import { isExecutedTrade } from "../lib/format";

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

export function PriceChartPanel({ symbol, trades = [] }: { symbol: string | null; trades?: TradeItem[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [barCount, setBarCount] = useState(0);

  useEffect(() => {
    if (!containerRef.current) return;
    const colors = readThemeColors();
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "transparent" }, textColor: colors.text, fontSize: 11 },
      grid: { vertLines: { color: colors.border }, horzLines: { color: colors.border } },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border },
      width: containerRef.current.clientWidth,
      height: 260,
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
    // resize). autoSize correctly picks up the new pixel width on a
    // hidden->visible transition, but does NOT itself re-fit the visible
    // time range afterward, leaving all bars compressed into whatever
    // narrow bar-spacing was last fit — most of the panel renders blank
    // with a cramped sliver of candles at one edge. Doing resize() and
    // fitContent() together, in that order, inside one observer removes
    // the ordering race a second independent observer would risk.
    const resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width && width > 0) {
        chart.resize(width, 260);
        chart.timeScale().fitContent();
      }
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
          clearChart();
          return;
        }
        const colors = readThemeColors();
        candleSeriesRef.current?.setData(toCandles(resp.bars));
        volumeSeriesRef.current?.setData(toVolume(resp.bars, colors));
        chartRef.current?.timeScale().fitContent();
        setBarCount(resp.bars.length);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
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
  const overlayText = !symbol
    ? "Click a candidate to chart it."
    : error
    ? `Could not load ${symbol} price history: ${error}`
    : !loading && barCount === 0
    ? `No daily bars available for ${symbol}.`
    : null;

  return (
    <Panel title={symbol ? `Price — ${symbol}` : "Price chart"} status={status} full>
      {/* Always mounted at a real, fixed size, never display:none — the
          chart object is created once against this container at mount
          time and lightweight-charts' autoSize ResizeObserver needs a
          real height to measure from the start. The overlay below sits on
          top of that same empty grid rather than adding a second block of
          vertical space beneath it, so an empty/degraded state reads as a
          designed placeholder instead of prime chart space going to waste
          on a blank "OK" box. */}
      <div className="relative">
        <div ref={containerRef} className="w-full h-[260px]" />
        {overlayText && (
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none px-4">
            <span
              className={`text-[0.82rem] text-center px-3 py-1.5 rounded-md border ${
                error ? "text-neg border-neg/40 bg-neg/10" : "text-dim border-border bg-panel/85"
              }`}
            >
              {overlayText}
            </span>
          </div>
        )}
      </div>
    </Panel>
  );
}
