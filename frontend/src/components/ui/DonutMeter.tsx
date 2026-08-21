import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { readQamcTheme } from "../../lib/theme";
import type { GaugeTone } from "./ArcGauge";

/* Part-to-whole composition donut (ECharts `pie` in donut mode) — the
 * replacement for the flat SegmentedBar liquidity/exposure bars, per
 * external review: those "read as thin horizontal lines" and don't
 * communicate composition the way a real allocation donut does. Chosen
 * over a treemap for these two specific panels because each is a genuinely
 * simple 2-3-category composition (liquidity: deployable/reserve/swept;
 * exposure: long/hedge/cash) — donuts are the right tool exactly there
 * ("keep it simple, limit categories"); a treemap earns its place instead
 * for the *positions* list (more holdings, a real hierarchy), not here.
 * Shares ArcGauge's tone system and EChart/theme substrate so the donut
 * and gauge read as one visual family, not two different chart libraries'
 * chrome bolted together. */

// Donuts render category composition, not a scalar gauge band, so "dim"
// (the same muted `--c-ink-dim` swatch the old SegmentedBar used via
// `bg-dim`) is a legitimate category color here for cash-equivalent/parked
// funds — deliberately neither a semantic status color (pos/warn/neg)
// nor the brand/chrome accent cyan, so SGOV sweep-parking never reads as
// either a market outcome or an interactive affordance.
export type DonutTone = GaugeTone | "dim";

export interface DonutSegment {
  label: string;
  value: number;
  tone: DonutTone;
}

export function DonutMeter({
  segments,
  centerLabel,
  centerValue,
  formatValue,
  height = 200,
}: {
  segments: DonutSegment[];
  /** Small caption above the center total, e.g. "TOTAL LIQUIDITY". */
  centerLabel: string;
  /** Pre-formatted center total — callers keep full control of formatting. */
  centerValue: string;
  formatValue: (v: number) => string;
  height?: number;
}) {
  const theme = readQamcTheme();
  const toneColor: Record<DonutTone, string> = {
    pos: theme.green,
    warn: theme.amber,
    neg: theme.red,
    accent: theme.accent,
    agent: theme.agent,
    hedge: theme.hedge,
    dim: theme.inkDim,
  };
  const positive = segments.filter((s) => s.value > 0);
  const total = positive.reduce((s, seg) => s + seg.value, 0);

  const option: EChartsOption = {
    tooltip: {
      trigger: "item",
      formatter: (params: unknown) => {
        const p = params as { name: string; value: number; percent: number };
        return `${p.name}: ${formatValue(p.value)} (${p.percent}%)`;
      },
    },
    series: [
      {
        type: "pie",
        radius: ["58%", "82%"],
        center: ["38%", "50%"],
        avoidLabelOverlap: true,
        label: { show: false },
        labelLine: { show: false },
        itemStyle: {
          borderColor: theme.panel,
          borderWidth: 2,
        },
        data: positive.map((s) => ({ name: s.label, value: s.value, itemStyle: { color: toneColor[s.tone] } })),
      },
    ],
    graphic: [
      {
        type: "text",
        left: "38%",
        top: "44%",
        style: {
          text: centerValue,
          align: "center",
          fill: theme.ink,
          fontFamily: "'IBM Plex Mono', monospace",
          fontWeight: 600,
          fontSize: 15,
        },
      },
      {
        type: "text",
        left: "38%",
        top: "58%",
        style: {
          text: centerLabel.toUpperCase(),
          align: "center",
          fill: theme.inkDim,
          fontFamily: "'IBM Plex Sans', sans-serif",
          fontWeight: 600,
          fontSize: 9,
        },
      },
    ],
  };

  return (
    <div className="flex items-center gap-2">
      <div className="w-[46%] flex-shrink-0">
        <EChart option={option} height={height} />
      </div>
      <div className="flex-1 flex flex-col gap-1.5 min-w-0">
        {segments.map((s) => {
          const pct = total > 0 ? (s.value / total) * 100 : 0;
          return (
            <div key={s.label} className="flex items-center gap-1.5 text-[0.8125rem] min-w-0">
              <span
                className="w-2 h-2 rounded-full flex-shrink-0"
                style={{ background: toneColor[s.tone] }}
              />
              <span className="text-dim truncate flex-1">{s.label}</span>
              <span className="font-mono num font-semibold flex-shrink-0">{formatValue(s.value)}</span>
              <span className="text-dim font-mono num flex-shrink-0 w-10 text-right">{pct.toFixed(0)}%</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
