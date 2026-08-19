import * as echarts from "echarts/core";
import { GaugeChart, ScatterChart, SankeyChart, GraphChart, FunnelChart } from "echarts/charts";
import { TooltipComponent, GridComponent, LegendComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

/* Tree-shaken ECharts registration — core + exactly the chart types QAMC
 * actually uses (gauge, scatter, sankey, graph, funnel) + one renderer,
 * never the full `echarts` bundle. Imported once here so every chart
 * component shares one registration instead of re-registering per
 * component. `FunnelChart` covers the candidate funnel — Tremor's
 * currently-published npm package (3.18.7) doesn't ship a FunnelChart
 * component (it's Tremor Raw/copy-paste-registry only), so the linear
 * 4-stage funnel uses ECharts' own native `funnel` series instead, which
 * is equally an approved mature tool per the design plan. */
echarts.use([GaugeChart, ScatterChart, SankeyChart, GraphChart, FunnelChart, TooltipComponent, GridComponent, LegendComponent, CanvasRenderer]);

export { echarts };
