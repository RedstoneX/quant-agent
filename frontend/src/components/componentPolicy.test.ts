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
    // Columns must still be shrinkable to near nothing rather than
    // carrying a header-tied minimum that shoves later columns out of
    // view. The mechanism changed on 2026-09-17 from TanStack's pixel
    // `minSize` to a minimum SHARE of the table width, because pixel
    // sizes summed past the panel width and silently clipped the last
    // columns — see the ColumnFractions comment in DataTable.tsx.
    expect(sourceModules["./ui/DataTable.tsx"]).toContain("MIN_COLUMN_FRACTION");
    expect(sourceModules["./ui/DataTable.tsx"]).toContain("truncate");
    expect(sourceModules["./HoldingsStrip.tsx"]).toContain("holdings-wrap");
    expect(sourceModules["./HoldingsStrip.tsx"]).not.toContain("overflow-x-auto");
    expect(sourceModules["./DesktopCockpitWorkspace.tsx"]).toContain("holdings");
    expect(sourceModules["./DesktopCockpitWorkspace.tsx"]).toContain('id: "account"');
    expect(sourceModules["./DesktopCockpitWorkspace.tsx"]).toContain('id: "sessions"');
    expect(sourceModules["./ui/DataTable.tsx"]).toContain("overflow-x-hidden");
    // Horizontal scrolling stays BANNED by default and is reachable only
    // through the explicit `scrollX` opt-in (owner request 2026-09-18,
    // board item 103: the Trades blotter's sixteen columns get both
    // scrollbars back, and no other table changes). Asserting the guard
    // expression itself — rather than just the absence of the class —
    // is what stops the old "every table grew a sideways scrollbar"
    // regression coming back while still allowing the one exception.
    expect(sourceModules["./ui/DataTable.tsx"]).toContain(
      'scrollX ? "table-scroll-x overflow-x-auto" : "overflow-x-hidden"',
    );
    expect(
      (sourceModules["./ui/DataTable.tsx"].match(/overflow-x-auto/g) ?? []).length,
    ).toBe(1);
    expect(sourceModules["./TodaySessionsStrip.tsx"]).not.toContain("overflow-x-auto");
    expect(sourceModules["./CandidateRail.tsx"]).not.toContain("overflow-x-auto");
    expect(sourceModules["./TradesPanel.tsx"]).toContain("DataTable");
    expect(sourceModules["./PriceChartPanel.tsx"]).toContain("lightweight-charts");
    expect(sourceModules["./DesktopCockpitWorkspace.tsx"]).toContain("DockviewReact");
  });
});
