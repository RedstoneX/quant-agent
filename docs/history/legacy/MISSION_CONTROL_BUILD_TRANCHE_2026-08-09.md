# Mission Control Build Tranche — Stages 3–5

Date: 2026-08-09

Operator decision: **AUTHORIZED**.

Checkpoint C / Stage 2 is accepted and merged. This document authorizes Claude Code to execute **Stages 3, 4 and 5 as one coordinated Mission Control engineering tranche** so the UI shell, AI-decision experience and journal/search can be planned and built with architectural continuity and maximum safe parallelism.

This is a bounded exception to the normal external STOP between every stage. It does **not** remove the stage definitions or their acceptance criteria.

## Authority and workflow

Claude Code is the engineering lead/orchestrator for this tranche. It owns implementation planning, worker/subagent selection, safe parallelism, worktree/file ownership, coding, integration, testing, debugging, implementation documentation and internal self-review.

Claude should detect the orchestration capabilities actually available in its current environment and use the strongest useful native mechanism without making the project depend on an experimental feature. Suitable mechanisms may include separate-context subagents, background workers/sessions, isolated git worktrees, or agent teams when they are genuinely available and useful. If an experimental mechanism is unavailable or troublesome, fall back immediately to simpler supported delegation rather than spending the tranche debugging orchestration infrastructure.

Do not micromanage the worker topology in advance. Partition work by interfaces/file ownership so parallel writers do not collide. The lead owns integration and conflict resolution.

## Internal stage gates

The existing stages remain meaningful review boundaries:

### Stage 3 — QAMC Native Cockpit
- React/Vite/Tailwind QAMC-native frontend;
- selective OpenTradex presentation/layout reuse only where cheaper than native implementation;
- TradingView Lightweight Charts;
- real Stage-2 API data for account/P&L, positions, orders, trades, candidates and health;
- responsive browser/iPad experience;
- no production mock data.

Internal gate: the existing Checkpoint D criteria must be self-verified and recorded before proceeding into Stage 4 work.

### Stage 4 — AI Decision Interface
- agent cards with role/provider/model/recommendation/confidence/reasoning/cost;
- disagreement/consensus visualization;
- PM proposal → AI Risk response → deterministic gate → executed/rejected delta;
- selectively adapt Orallexa concepts/components only where cheaper than native implementation;
- one candidate/decision can be followed end-to-end without reading raw logs.

Internal gate: Stage 4 acceptance criteria must be self-verified and recorded before proceeding into Stage 5 work.

### Stage 5 — Native Journal & Indexed Search
- calendar/list/structured daily journal views derived from canonical QAMC data;
- daily sections required by `docs/architecture/JOURNAL_AND_SEARCH.md`;
- server-side indexed structured/full-text search over a rebuildable, non-authoritative read model;
- visible validated filters if natural-language translation is implemented;
- no arbitrary LLM-generated SQL;
- no second trading-memory system.

Internal gate: Checkpoint E criteria must be self-verified before tranche completion.

## Tranche publication discipline

At each internal stage boundary Claude must:
1. run the targeted tests appropriate to that stage;
2. perform a fresh-context self-review with no authorship bias where useful;
3. update the implementation documentation needed to make the state reconstructable;
4. create a clear commit boundary/checkpoint record so ChatGPT can independently inspect Stage 3, Stage 4 and Stage 5 afterward.

For this authorized tranche, Claude **may continue from Stage 3 → Stage 4 → Stage 5 after an internal gate is green without waiting for intermediate operator/ChatGPT acceptance**. This exception exists specifically to avoid wasting orchestration/context continuity across tightly coupled Mission Control frontend work.

Claude must **STOP after Stage 5 / Checkpoint E self-verification**. Do not merge to `main`. Push the completed tranche branch and report stage-boundary commits, tests, screenshots/visual verification evidence, architecture choices and unresolved limitations. ChatGPT will then independently review the actual GitHub branch and the operator will accept or reject the tranche before merge.

Stages 7–9 remain outside this authorization. Stage 8 write controls are especially not authorized by this tranche.

## Visual verification

This is UI-heavy work. Claude should not treat passing unit tests as sufficient. Use the browser/preview/rendering capability actually available in the environment to inspect the running UI, including desktop and iPad-sized layouts, empty/error/loading states and real API-backed screens. Prefer automated browser/render verification when practical. Do not create a large bespoke visual-test framework merely to satisfy this instruction.

## Safety and architecture invariants

The following remain non-negotiable throughout Stages 3–5:
- `yebof/quant-agent` remains the authoritative trading engine;
- Alpaca Paper only; live trading is not authorized;
- deterministic Python remains final risk/execution authority;
- Mission Control must not become a second trading engine;
- frontend/API/journal/search failure must not affect trading or broker protection;
- the Stage-2 API remains read-only; frontend code cannot place/cancel/modify/bypass orders;
- no secrets in frontend/API responses;
- no production mock data masquerading as live state;
- journal/search derived state is rebuildable and non-authoritative;
- no Redis/Kafka/Kubernetes/PostgreSQL/MongoDB or other infrastructure without an approved demonstrated need;
- AgentLens remains out of plan;
- do not import donor backend/trading assumptions into QAMC.

## Stop/escalation conditions

Stop and report rather than silently expanding scope if any of the following is required:
- a change to deterministic risk/execution behavior;
- a write-capable Mission Control API;
- a broad or safety-sensitive canonical-schema redesign not already governed by Stage 5;
- a new distributed service/infrastructure dependency;
- an architectural conflict with accepted decisions;
- inability to verify that a UI representation is backed by real canonical/API data;
- an optional donor/integration becoming a substantial bespoke project.

This document supersedes temporary Stage-3/4/5 blocking language in older governance text only for this explicitly authorized Mission Control build tranche. It does not authorize Stage 7+, live trading, or write operations.