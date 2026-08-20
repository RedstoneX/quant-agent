import * as echarts from "echarts/core";
import { GaugeChart, ScatterChart, SankeyChart, GraphChart, FunnelChart, PieChart, TreemapChart } from "echarts/charts";
import { TooltipComponent, GridComponent, LegendComponent, GraphicComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

/* Tree-shaken ECharts registration — core + exactly the chart types QAMC
 * actually uses (gauge, scatter, sankey, graph, funnel, pie/donut, treemap)
 * + one renderer, never the full `echarts` bundle. Imported once here so
 * every chart component shares one registration instead of re-registering
 * per component. `FunnelChart` covers the candidate funnel — Tremor's
 * currently-published npm package (3.18.7) doesn't ship a FunnelChart
 * component (it's Tremor Raw/copy-paste-registry only), so the linear
 * 4-stage funnel uses ECharts' own native `funnel` series instead, which
 * is equally an approved mature tool per the design plan. `PieChart` (used
 * in donut mode) covers the liquidity/exposure composition donuts — chosen
 * over Tremor's DonutChart so it shares one visual family (and one
 * center-label mechanism) with ArcGauge rather than mixing two libraries'
 * chart chrome for adjacent panels. `TreemapChart` covers position-level
 * concentration (size = |market value|, color = P&L sign) — the standard
 * portfolio-composition pattern (Finviz map, Morningstar X-ray) and a
 * genuinely better fit than a donut once holdings are a real hierarchy
 * rather than 2-3 buckets, per the same "simple composition -> donut, real
 * hierarchy -> treemap" rule DonutMeter's own header comment documents.
 * `ScatterChart`/`SankeyChart` were registered ahead of use (candidate
 * funnel work) and are reused as-is. `GraphicComponent` backs ArcGauge's/
 * DonutMeter's center-label `graphic` elements — omitting it doesn't break
 * rendering (ECharts warns and skips the graphic) but throws a console
 * error on every affected panel, which fails this project's zero-console-
 * error verification bar. */
echarts.use([
  GaugeChart,
  ScatterChart,
  SankeyChart,
  GraphChart,
  FunnelChart,
  PieChart,
  TreemapChart,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  GraphicComponent,
  CanvasRenderer,
]);

export { echarts };
