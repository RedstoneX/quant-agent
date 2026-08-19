import { useEffect, useRef } from "react";
import type { EChartsOption } from "echarts";
import { echarts } from "../../lib/echarts";

/* Minimal generic ECharts-in-React mount — deliberately not the
 * `echarts-for-react` package (one fewer dependency for a ~40-line wrapper
 * QAMC fully owns): creates the chart once, applies `option` on every
 * change via `setOption(option, true)` (the `true` replaces rather than
 * merges, so a changed series doesn't leave stale visual elements behind),
 * and resizes with the container via ResizeObserver — the same pattern
 * PriceChartPanel already uses for lightweight-charts. */
export function EChart({
  option,
  height = 160,
  className,
}: {
  option: EChartsOption;
  height?: number | string;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(containerRef.current);
    return () => {
      resizeObserver.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={containerRef} className={className} style={{ width: "100%", height }} />;
}
