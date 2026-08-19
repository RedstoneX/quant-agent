import formsPlugin from "@tailwindcss/forms";

// Tremor's components pick their color from its own fixed Tailwind-palette
// name set (e.g. `color="emerald"`) rather than QAMC's CSS-variable tokens,
// and purges anything not statically found in source — this safelist keeps
// exactly the palette names QAMC's own hue grammar maps onto (emerald/rose/
// amber for market-truth+warn, violet for AI reasoning, fuchsia for the
// hedge flag, cyan for accent, slate for neutral) instead of the vendored
// docs' full 22-color safelist, which would bloat the CSS with 21 unused
// palettes.
const TREMOR_COLORS = ["slate", "cyan", "emerald", "amber", "rose", "violet", "fuchsia"];
const TREMOR_SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];
const tremorSafelist = TREMOR_COLORS.flatMap((color) =>
  TREMOR_SHADES.flatMap((shade) => [
    { pattern: new RegExp(`^bg-${color}-${shade}$`) },
    { pattern: new RegExp(`^text-${color}-${shade}$`) },
    { pattern: new RegExp(`^border-${color}-${shade}$`) },
    { pattern: new RegExp(`^ring-${color}-${shade}$`) },
    { pattern: new RegExp(`^stroke-${color}-${shade}$`) },
    { pattern: new RegExp(`^fill-${color}-${shade}$`) },
  ])
);

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "media",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}", "./node_modules/@tremor/**/*.{js,ts,jsx,tsx}"],
  safelist: tremorSafelist,
  theme: {
    extend: {
      fontFamily: {
        sans: ["'IBM Plex Sans'", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Helvetica", "Arial", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        bg: "rgb(var(--c-bg) / <alpha-value>)",
        panel: "rgb(var(--c-surface) / <alpha-value>)",
        "panel-alt": "rgb(var(--c-surface-2) / <alpha-value>)",
        "panel-inset": "rgb(var(--c-surface-inset) / <alpha-value>)",
        border: "rgb(var(--c-border) / <alpha-value>)",
        "border-strong": "rgb(var(--c-border-strong) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        dim: "rgb(var(--c-ink-dim) / <alpha-value>)",
        faint: "rgb(var(--c-ink-faint) / <alpha-value>)",
        // System/brand/interactive chrome — navigation, links, focus, the
        // deterministic-gate accent. Never used for AI reasoning or P&L.
        accent: "rgb(var(--c-accent) / <alpha-value>)",
        // Market truth — P&L, price direction. Conventional green/red only;
        // never repurposed for anything else so the eye can trust the hue.
        pos: "rgb(var(--c-green) / <alpha-value>)",
        neg: "rgb(var(--c-red) / <alpha-value>)",
        // Attention / pending / modified-by-risk.
        warn: "rgb(var(--c-amber) / <alpha-value>)",
        // AI reasoning — specialists, PM, AI Risk Manager chrome. Deliberately
        // distinct from `accent` (system chrome) and `pos`/`neg` (market
        // truth) so color itself communicates "this is a model's judgment."
        agent: "rgb(var(--c-agent) / <alpha-value>)",
        // Bearish-hedge instrument flag (SH/SDS/PSQ/SQQQ) — distinct from
        // `agent` so a hedge badge next to a specialist card never reads as
        // "this is the AI talking."
        hedge: "rgb(var(--c-hedge) / <alpha-value>)",
      },
    },
  },
  plugins: [formsPlugin],
};
