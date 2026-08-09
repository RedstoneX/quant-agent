# Approved Donor Inventory

This file is the gate against unnecessary custom UI work. Stage 0 must replace broad descriptions with exact files/components/commits after inspection.

> **Stage 0 inspection status — COMPLETE (2026-08-09).** All donors are now
> identified and pinned:
>
> | Donor | Repository | Pinned commit | License | Verdict |
> |---|---|---|---|---|
> | OpenTradex | `deonmenezes/opentradex` | `30b23f5ec3ad59ceecdd0335af2c5513c4137d36` | MIT | keep — layout/visual donor |
> | Orallexa | `alex-jb/orallexa-ai-trading-agent` | `794a2ec0ce0b1271b468814eee47c2cd4edde147` | MIT | **keep — concepts verified to exist** |
> | TradingView Lightweight Charts | `tradingview/lightweight-charts` | n/a (library dependency) | Apache-2.0 | keep |
> | AgentLens | `tranhoangtu-it/agentlens` | `21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f` | MIT | **DROP recommended** — see `docs/architecture/AGENTLENS.md` |
>
> Full evidence: `docs/STAGE0_BASELINE_AUDIT.md` §8, §8A, §8B, §8C.

## OpenTradex — primary trading UI donor
Repository: `deonmenezes/opentradex` (MIT, © 2026 Deon Menezes).

**Inspected at commit `30b23f5ec3ad59ceecdd0335af2c5513c4137d36` (2026-07-07).**
Dashboard stack is React 18 + Vite 5 + Tailwind 3 + TypeScript 5 — an exact
match for DECISION #13. All five named components exist and couple to
OpenTradex's domain **by type import only**, so adaptation means redefining a
type rather than rewriting logic.

| File (`packages/dashboard/src/`) | Lines | Non-React imports | Stage 0 note |
|---|---|---|---|
| `components/Resizable.tsx` | 51 | none | thin wrapper over `react-resizable-panels`; depending on that library directly is likely cheaper than vendoring |
| `components/HarnessStatusBadges.tsx` | 181 | `type AgentContext` | reusable status-badge pattern |
| `components/RunsAuditPanel.tsx` | 89 | `type SkillRun` | presents *skill runs*, not trades |
| `components/AgentConsole.tsx` | 189 | `type SkillRun` | a chat/skill-run console, **not** a per-analyst recommendation card |
| `components/FlowVisualizer.tsx` | 238 | `type Skill, SkillRun, InvokeResult` + `categoryStyle` | closest analogue to a decision-chain view |
| `components/ConfirmModal.tsx` | 112 | `type Skill` | reusable |
| `components/TopBar.tsx` | 414 | `type HarnessStatus, WsMeta, AgentContext` | layout pattern |
| `components/LeftSidebar.tsx` | 331 | `type Position, Trade, Market` | the only component in finance vocabulary |

**Stage 0 qualification.** OpenTradex's domain vocabulary is
Skills / SkillRuns / Harness / agent chat — a prediction-market agent harness,
not a stock-portfolio cockpit. The realistic donation is **layout primitives
and visual language**, not the agent-semantics components.
`docs/ui/UI_COMPONENT_MAP.md` currently over-promises on that mapping.

Prefer adapting presentation-only assets such as:
- resizable panel/layout primitives;
- cockpit navigation/sidebar concepts;
- cards, tables, status badges, confirm modals;
- run/audit/status presentation;
- responsive terminal visual language.

Known component examples to inspect: `Resizable`, `AgentConsole`, `FlowVisualizer`, `HarnessStatusBadges`, `RunsAuditPanel`, sidebars, confirmation/command presentation.

Explicitly discard:
- `useHarness` and OpenTradex-specific API/data normalization;
- gateway/trading engine;
- Kalshi/Polymarket/scraper assumptions;
- OpenTradex risk/execution models;
- any backend contract that forces quant-agent to pretend to be OpenTradex.

## Orallexa — multi-agent UI donor
Repository: `alex-jb/orallexa-ai-trading-agent` (MIT, © 2026 Orallexa Team).

**Inspected at commit `794a2ec0ce0b1271b468814eee47c2cd4edde147` (2026-07-12).**
Frontend is `orallexa-ui/` — **Next.js 16 App Router + React 19 + Tailwind 4**,
and it already depends on `lightweight-charts` ^5.1.0, the same charting
foundation as DECISION #16. Not Vite (DECISION #13), but the components are
plain `"use client"` React; the only Next-specific import in an adoption
candidate is `next/image`.

**Every proposed concept was verified to exist**, with one exception. Coupling
is `types` (type-only import from `app/types.ts` plus pure helpers), `atoms`
(local primitives), or `fetch` (component performs its own HTTP call).

| Concept | File → export | Lines | Coupling | Priority |
|---|---|---|---|---|
| Analyst cards + disagreement/consensus | `app/components/scenario-panel.tsx` → `PerspectivePanelCard` | 385 (file) | `types`, `atoms`, **`fetch`** | **1 — best match in either donor** |
| Portfolio/risk verdict card | `app/components/portfolio-manager-card.tsx` → `PortfolioManagerCard` | 112 | `types`, `atoms` | **2 — but see naming inversion** |
| Token/cost budget | `app/components/token-budget-badge.tsx` → `TokenBudgetBadge` | 108 | `types` only | **3 — cleanest lift in the repo** |
| Model scoreboard | `app/components/ml-scoreboard.tsx` → `MLScoreboard` | 218 | `types`, `atoms` | 4 |
| Layout/status primitives | `app/components/atoms.tsx` (`Mod`, `Heading`, `Row`, `GoldRule`, `Toggle`, `CopyBtn`, …) | 194 | `next/image`, `types` | 4 |
| Recommendation/confidence | `app/components/decision-card.tsx` → `DecisionCard` | 249 | `types`, `atoms`, `next/image` | 5 |
| Weighted signal fusion | `app/components/signal-fusion.tsx` → `SignalFusionCard` | 258 | `types`, `atoms` | 5 |
| Watchlist/candidates | `app/components/watchlist.tsx` → `WatchlistGrid` | 119 | `types` | 5 |
| Regime card | `app/components/regime-card.tsx` → `RegimeCard` | 140 | `atoms` | 5 |
| Error boundary, market strip | `error-boundary.tsx` (81), `market-strip.tsx` (49) | — | minimal | 5 |
| Daily intelligence | `app/components/daily-intel.tsx` → `DailyIntelView` | 844 | heavy | **skip** — too large and Orallexa-specific |
| **Decision-chain presentation** | — | — | — | **absent — build native** |

`PerspectivePanelCard` renders consensus + agreement %, a divergence bar, and
one row per analyst role (icon, role, bias badge, score, reasoning, conviction,
key factor, historical-accuracy badge). Its `PerspectiveView` type maps almost
one-to-one onto QAMC's four specialist analysts.

**Four adaptation costs, all known:**
1. **Naming inversion.** Orallexa's `PortfolioManagerCard` presents
   approve/reject + `scaled_position_pct` + warnings + confidence adjustment —
   in QAMC's vocabulary those are the **AI Risk Manager's** semantics, not the
   PM's. Wire it to `risk_manager`.
2. **Two components self-fetch** (`scenario-panel.tsx:292` hits
   `/api/role-memory`; `bias-tracker.tsx` likewise). Lift to props.
3. **Bilingual props ride along.** Every candidate takes `t: Record<string,string>`
   and most take `zh: boolean`. QAMC needs neither; stripping them touches most
   JSX lines in an adopted file.
4. **No theme layer.** Styling is inline hex (gold `#D4AF37`, cream `#F5E6CA`)
   plus Tailwind arbitrary values. Adopting a component adopts Orallexa's
   art-deco identity, or means rewriting every `className`.

**Posture: adapt, do not vendor.** Do not import its Python trading engine,
`llm/`, `engine/`, `portfolio/`, `rag/`, `markets/`, memory system,
`api_server.py`, provider architecture, or mock/demo data paths — none of which
was evaluated for adoption.

## TradingView Lightweight Charts — charting foundation
Use for candlesticks, volume, indicators and trade markers. Preserve required attribution/license obligations.

Stage 0: `tradingview/lightweight-charts` confirmed reachable; no component
inspection performed or needed — it is a published library, and adding the
dependency (with its attribution obligation) is the whole integration.

## QuantDinger
Visual/design inspiration only by default.

## Trading journal donor
Information architecture only; no unlicensed code copy. Do not import Firebase/auth/storage architecture.

## Rule
Before building a component from scratch, check this inventory. Reuse only when adapting the component is simpler than reproducing it natively and does not import unwanted architecture.
