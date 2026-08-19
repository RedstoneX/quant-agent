// Resolves QAMC's CSS custom-property tokens (space-separated "R G B", the
// Tailwind arbitrary-alpha convention) to literal comma-separated
// rgb()/rgba() strings. Needed anywhere a color is handed to a CANVAS-based
// renderer with its own internal color parser (ECharts, lightweight-charts)
// rather than the browser's CSS engine — those parsers cannot resolve
// `var(--c-x)` at all, unlike real CSS properties/SVG attributes. See
// PriceChartPanel.tsx's `readThemeColors` for the original instance of this
// same requirement; this generalizes it for ECharts option-builders.
export function resolveThemeColor(varName: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return `rgb(${raw.split(/\s+/).join(", ")})`;
}

export function resolveThemeColorAlpha(varName: string, alpha: number): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
  return `rgba(${raw.split(/\s+/).join(", ")}, ${alpha})`;
}

export interface QamcTheme {
  bg: string;
  panel: string;
  panelInset: string;
  border: string;
  borderStrong: string;
  ink: string;
  inkDim: string;
  accent: string;
  agent: string;
  green: string;
  red: string;
  amber: string;
  hedge: string;
}

export function readQamcTheme(): QamcTheme {
  return {
    bg: resolveThemeColor("--c-bg"),
    panel: resolveThemeColor("--c-surface"),
    panelInset: resolveThemeColor("--c-surface-inset"),
    border: resolveThemeColor("--c-border"),
    borderStrong: resolveThemeColor("--c-border-strong"),
    ink: resolveThemeColor("--c-ink"),
    inkDim: resolveThemeColor("--c-ink-dim"),
    accent: resolveThemeColor("--c-accent"),
    agent: resolveThemeColor("--c-agent"),
    green: resolveThemeColor("--c-green"),
    red: resolveThemeColor("--c-red"),
    amber: resolveThemeColor("--c-amber"),
    hedge: resolveThemeColor("--c-hedge"),
  };
}
