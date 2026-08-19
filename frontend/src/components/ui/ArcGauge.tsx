import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { readQamcTheme } from "../../lib/theme";

/* Segmented-color-band arc gauge (ECharts `gauge` series —
 * axisLine.lineStyle.color takes a [[threshold, color], ...] array) for
 * exposure/risk/liquidity percentages in the hero band. Chosen over a flat
 * CSS bar specifically because the operator's complaint was that exposure
 * "lacks strong graphical hierarchy" — a real instrument-panel gauge reads
 * at a glance the way a bar can't.
 *
 * Colors are resolved from QAMC's CSS custom properties to literal
 * rgb()/rgba() strings via `readQamcTheme()` — ECharts' canvas color
 * parser (like lightweight-charts') cannot resolve `var(--c-x)` the way a
 * real CSS property can; see lib/theme.ts. */

export type GaugeTone = "pos" | "warn" | "neg" | "accent" | "agent" | "hedge";

export interface GaugeBand {
  /** Cumulative threshold (0-100) where this color band ends. */
  upTo: number;
  tone: GaugeTone;
}

export function ArcGauge({
  value,
  label,
  valueLabel,
  bands,
  height = 130,
}: {
  /** 0-100 */
  value: number;
  label: string;
  /** Pre-formatted center text (e.g. "62%" or "$78.9k") — never re-derived
   * inside the chart so callers keep full control of number formatting. */
  valueLabel: string;
  bands: GaugeBand[];
  height?: number;
}) {
  const theme = readQamcTheme();
  const toneColor: Record<GaugeTone, string> = {
    pos: theme.green,
    warn: theme.amber,
    neg: theme.red,
    accent: theme.accent,
    agent: theme.agent,
    hedge: theme.hedge,
  };

  const option: EChartsOption = {
    series: [
      {
        type: "gauge",
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        radius: "92%",
        center: ["50%", "62%"],
        progress: { show: false },
        pointer: { show: false },
        axisLine: {
          lineStyle: {
            width: 10,
            color: bands.map((b) => [b.upTo / 100, toneColor[b.tone]] as [number, string]),
          },
        },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        anchor: { show: false },
        detail: {
          valueAnimation: true,
          formatter: () => valueLabel,
          color: theme.ink,
          fontFamily: "'IBM Plex Mono', monospace",
          fontWeight: 600,
          fontSize: 20,
          offsetCenter: [0, "-6%"],
        },
        title: {
          show: true,
          offsetCenter: [0, "38%"],
          color: theme.inkDim,
          fontFamily: "'IBM Plex Sans', sans-serif",
          fontSize: 10.5,
          fontWeight: 600,
        },
        data: [{ value, name: label.toUpperCase() }],
      },
      // A second, needle-only gauge series draws the real position marker
      // as a short radial tick rather than ECharts' default pointer shape
      // — reads as "here's where we are on the track," not a speedometer.
      {
        type: "gauge",
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        radius: "92%",
        center: ["50%", "62%"],
        pointer: {
          show: true,
          icon: "rect",
          width: 3,
          length: 14,
          offsetCenter: [0, "-92%"],
          itemStyle: { color: theme.ink },
        },
        progress: { show: false },
        axisLine: { show: false, roundCap: false, lineStyle: { width: 0, color: [[1, "transparent"]] } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        detail: { show: false },
        title: { show: false },
        anchor: { show: true, size: 5, itemStyle: { color: theme.ink, borderColor: theme.border, borderWidth: 1 } },
        data: [{ value }],
      },
    ],
  };

  return <EChart option={option} height={height} />;
}
