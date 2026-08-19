import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { readQamcTheme } from "../../lib/theme";
import { PositionItem } from "../../api/client";
import { fmtMoneyCompact, fmtPct } from "../../lib/format";

/* Position-level concentration + winner/loser shape — the "visualization
 * for portfolio shape, table for precise values" half of PositionsPanel.
 * A treemap (not another donut) because holdings are a real, un-bucketed
 * list rather than 2-3 composition categories — DonutMeter's own header
 * comment documents that same "simple composition -> donut, real
 * hierarchy -> treemap" split. Block area = |market value| (concentration);
 * block color = unrealized P&L sign, using `pos`/`neg` (green/red) exactly
 * as the design-token grammar defines them — this is real market truth
 * (an actual gain/loss), never a repurposed status color. Cash-equivalent
 * sweep positions (SGOV) render `dim`, matching LiquidityPanel's donuts,
 * since they carry no directional P&L thesis to color by. */

export function PositionsTreemap({ positions, height = 200 }: { positions: PositionItem[]; height?: number }) {
  const theme = readQamcTheme();
  const sized = positions.filter((p) => Math.abs(p.market_value) > 0);

  const data = sized.map((p) => {
    const pct = p.avg_entry ? ((p.current_price - p.avg_entry) / p.avg_entry) * 100 : 0;
    const color = p.is_cash_equivalent ? theme.inkDim : p.unrealized_pnl > 0 ? theme.green : p.unrealized_pnl < 0 ? theme.red : theme.inkDim;
    return {
      name: p.symbol,
      value: Math.abs(p.market_value),
      pnl: p.unrealized_pnl,
      pnlPct: pct,
      marketValue: p.market_value,
      isCashEquivalent: p.is_cash_equivalent,
      itemStyle: { color },
    };
  });

  const option: EChartsOption = {
    tooltip: {
      formatter: (params: unknown) => {
        const p = params as { data: { name: string; marketValue: number; pnl: number; pnlPct: number; isCashEquivalent: boolean } };
        const d = p.data;
        if (d.isCashEquivalent) return `<b>${d.name}</b><br/>${fmtMoneyCompact(d.marketValue)} · cash-equivalent`;
        return `<b>${d.name}</b><br/>${fmtMoneyCompact(d.marketValue)}<br/>${fmtMoneyCompact(d.pnl)} (${fmtPct(d.pnlPct)})`;
      },
    },
    series: [
      {
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        label: {
          show: true,
          formatter: (params: unknown) => {
            const p = params as { name: string; data: { pnlPct: number; isCashEquivalent: boolean } };
            if (p.data.isCashEquivalent) return `{sym|${p.name}}`;
            return `{sym|${p.name}}\n{pct|${fmtPct(p.data.pnlPct)}}`;
          },
          rich: {
            sym: { fontFamily: "'IBM Plex Sans', sans-serif", fontWeight: 700, fontSize: 12, color: theme.bg, lineHeight: 16 },
            pct: { fontFamily: "'IBM Plex Mono', monospace", fontWeight: 600, fontSize: 10.5, color: theme.bg, lineHeight: 14 },
          },
        },
        upperLabel: { show: false },
        itemStyle: { borderColor: theme.panel, borderWidth: 2, gapWidth: 2 },
        levels: [{ itemStyle: { borderColor: theme.panel, borderWidth: 2, gapWidth: 2 } }],
        data,
      },
    ],
  };

  return <EChart option={option} height={height} />;
}
