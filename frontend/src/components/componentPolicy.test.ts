import { describe, expect, it } from "vitest";
import packageJson from "../../package.json";

const sourceModules = import.meta.glob("../**/*.{ts,tsx}", { eager: true, query: "?raw", import: "default" }) as Record<string, string>;

describe("Mission Control component policy", () => {
  it("has no ECharts dependency or ordinary raw HTML tables", () => {
    expect("echarts" in packageJson.dependencies).toBe(false);
    const source = Object.entries(sourceModules)
      .filter(([path]) => !path.endsWith("componentPolicy.test.ts"))
      .map(([, contents]) => contents)
      .join("\n");
    expect(source).not.toMatch(/\bECharts?\b|DonutMeter|PositionsTreemap/);
    expect(source).not.toContain("<table");
  });

  it("keeps the approved table, financial-chart and desktop-workspace components", () => {
    expect(sourceModules["./PositionsPanel.tsx"]).toContain("DataTable");
    expect(sourceModules["./OrdersPanel.tsx"]).toContain("DataTable");
    expect(sourceModules["./TradesPanel.tsx"]).toContain("DataTable");
    expect(sourceModules["./PriceChartPanel.tsx"]).toContain("lightweight-charts");
    expect(sourceModules["./DesktopCockpitWorkspace.tsx"]).toContain("DockviewReact");
  });
});
