import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes straight into src/api/server.py's existing /cockpit
// StaticFiles mount (src/api/static_cockpit/) — same no-build-step-at-
// deploy-time posture as /ui: qamc's deployment serves committed static
// files, it never runs npm. Only `dev`'s checkout ever runs `npm run
// build` to regenerate that directory. See frontend/README.md.
export default defineConfig({
  plugins: [react()],
  base: "/cockpit/",
  build: {
    outDir: "../src/api/static_cockpit",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies API calls to the live local Mission Control
    // API so the cockpit can be developed against real read-only data
    // without a build step.
    proxy: {
      "^/(account|positions|orders|trades|runs|decisions|agents|reflections|candidates|journal|search|health|prices|quotes)": {
        target: "http://127.0.0.1:8800",
        changeOrigin: true,
      },
    },
  },
});
