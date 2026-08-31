# QAMC Mission Control — cockpit frontend

React + TypeScript + Vite + Tailwind, plus TradingView Lightweight Charts
for the price panel. This is the source for the cockpit served by
`src/api/server.py`'s `/cockpit` mount (`src/api/static_cockpit/`).

Authorized in `AGENTS.md`'s shipped-tranche acceptance contract ("Final
Mission Control direction" / Workstream B) as a bounded, in-repo frontend replacement for the legacy
`/ui` dashboard — evaluated against React+Vite+Tailwind+TradingView
Lightweight Charts (the original QAMC design direction, see the Stage 0
donor inventory in Git history) rather than continuing the vanilla-JS
prototype that preceded this build (preserved in Git history at commit
`73c68bf`, with its Stage 6 acceptance evidence likewise preserved in
Git history — its information architecture carried over into
this rebuild, its vanilla-JS implementation did not).

## Deployment model — no new runtime dependency

This is a **build-time** dependency only. The compiled output
(`dist/` → `../src/api/static_cockpit/`, committed to Git) is what
actually ships: plain static HTML/CSS/JS served by the same
`StaticFiles` mount `/ui` already uses. `qamc`'s deployment never runs
`npm`/`node` — only this `dev` checkout does, when the frontend source
changes. There is no new server, service, or backend dependency.

## Commands

```bash
npm install       # first time / after a dependency change
npm run dev       # dev server at :5173, proxies API calls to the local
                   # read-only Mission Control API on :8800 (see vite.config.ts)
npm run build     # type-checks, then builds into src/api/static_cockpit/
npm run preview   # serve the built output locally for a final check
```

## Structure

- `src/api/client.ts` — typed fetch wrappers mirroring
  `src/api/schemas.py`'s Pydantic response models exactly (field-for-field
  — keep the two in sync by hand when the backend contract changes).
- `src/components/` — one file per cockpit panel/card, plus the modal
  shell used for run/candidate drill-down.
- `src/App.tsx` — layout + polling orchestration (mirrors the legacy
  dashboards' `REFRESH_MS` polling posture — no websocket, no new
  transport).

## Boundaries this frontend must not cross

Same as every other Mission Control surface (`docs/architecture/
MISSION_CONTROL_API.md`, `docs/OUTCOME.md`): read-only, no broker-write
controls (no PAUSE/KILL/trade buttons — those remain explicitly out of
scope), no fabricated data, no second trading-memory system. It consumes
only the existing GET-only API; any new backend field/endpoint it needs
is added there under the same isolation invariants, never inline here.
