# Approved Donor Inventory

This file is the gate against unnecessary custom UI work. Stage 0 must replace broad descriptions with exact files/components/commits after inspection.

> **Stage 0 inspection status (2026-08-09).** OpenTradex inspected at an exact
> commit — see the table below. **Orallexa and AgentLens could not be inspected:
> no governed document records a repository, license or commit for either**
> (discrepancies D-2 / D-3). TradingView Lightweight Charts was confirmed
> reachable but not inspected — it is a published library, and depending on it
> *is* the integration. Full evidence: `docs/STAGE0_BASELINE_AUDIT.md` §8.

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
**Repository: NOT RECORDED. Stage 0 could not inspect this donor (D-2).**
No governed document names a repository, license or commit. A web search
surfaced a plausible candidate, `alex-jb/orallexa-ai-trading-agent` (reachable),
whose description matches the concepts cited below — but this is an inference,
was **not** confirmed, and was **not** audited. Auditing an unconfirmed
repository is exactly the unbounded donor work `AGENTS.md` forbids. The
operator must name (or drop) this donor before any Stage 3/4 inspection.

Use/adapt where implementation is genuinely reusable:
- individual analyst cards;
- recommendation/confidence presentation;
- disagreement/debate/signal-fusion visualization;
- Portfolio Manager and risk decision cards;
- model scoreboard, token/cost budget concepts;
- daily intelligence/watchlist patterns.

Do not import its trading logic or mock/demo data paths.

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
