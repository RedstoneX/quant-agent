/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "media",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--c-bg) / <alpha-value>)",
        panel: "rgb(var(--c-panel) / <alpha-value>)",
        "panel-alt": "rgb(var(--c-panel-alt) / <alpha-value>)",
        border: "rgb(var(--c-border) / <alpha-value>)",
        ink: "rgb(var(--c-text) / <alpha-value>)",
        dim: "rgb(var(--c-text-dim) / <alpha-value>)",
        accent: "rgb(var(--c-accent) / <alpha-value>)",
        pos: "rgb(var(--c-green) / <alpha-value>)",
        neg: "rgb(var(--c-red) / <alpha-value>)",
        warn: "rgb(var(--c-amber) / <alpha-value>)",
        hedge: "rgb(var(--c-purple) / <alpha-value>)",
      },
    },
  },
  plugins: [],
};
