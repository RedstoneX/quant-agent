import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, CandlestickData, HistogramData } from "lightweight-charts";
import { api, PriceBar } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";

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
  const text = rgb("--c-text-dim");
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

export function PriceChartPanel({ symbol }: { symbol: string | null }) {
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
      height: 260,
      autoSize: true,
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

    return () => {
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

  const status = error ? "error" : loading ? "loading" : "ok";
  return (
    <Panel title={symbol ? `Price — ${symbol}` : "Price chart"} status={status} full>
      {!symbol && <StateMessage text="Click a candidate above to chart it." />}
      {symbol && error && <StateMessage text={`Could not load ${symbol} price history: ${error}`} error />}
      {symbol && !error && barCount === 0 && !loading && (
        <StateMessage text={`No daily bars available for ${symbol}.`} />
      )}
      {/* Always mounted at a real, fixed size, never display:none — the
          chart object is created once against this container at mount
          time and lightweight-charts' autoSize ResizeObserver needs a
          real height to measure from the start. An empty grid with no
          candles when nothing is loaded yet is an honest empty state,
          not fabricated data. */}
      <div ref={containerRef} className="w-full h-[260px]" />
    </Panel>
  );
}
