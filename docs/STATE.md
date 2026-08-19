# QAMC Current State

Updated: 2026-08-19

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC reaches Alpaca Paper through the approved OneCLI path; runtime checks report `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- Alpaca Paper-only is enforced in code.
- Cost-optimized model routing and the decision-chain audit are accepted on `main`.
- PR #33 fixed the commissioning timer-state false positive and merged to `main` as `aa52f5f9fd5912914a1640f74bdab84d1e30cd51`.
- PR #37 recorded the verified private Tailscale/Orca access path and merged to `main` on 2026-08-18.
- PR #46 (Mission Control cockpit redesign and visualization upgrade, Stages 6–6h) merged to `main` on 2026-08-19. Mission Control production cutover completed successfully the same day; production is at `766877109b60026c94c00b38dbfb0e0c9630d236` (`7668771`). `/cockpit` and `/ui` are both confirmed healthy. Alpaca remains Paper-only.

## Current authorization — Telegram notification restoration

A bounded tranche is authorized to **restore, configure and test the existing upstream Telegram notification capability** already present in `src/notifier.py`, `main.py`, `src/scheduler.py` and the existing run wrappers. This is restoration/integration, not a new product or subsystem.

Allowed work is limited to inspecting existing notifier/deployment wiring, documenting/restoring `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / optional `TELEGRAM_DISABLED` configuration without exposing secrets, making only minimal reliability/configuration/test changes, and proving notification delivery with a non-trading test. Telegram remains status/notification output only and notifier failure must remain non-fatal to trading.

This authorization does **not** permit Telegram commands/callbacks/webhook trading controls, any broker-write path, a new daemon/service/database/proxy/security/credential architecture, live trading, margin/options/direct stock shorting, production deployment of PR #48, or enabling `intraday_scan`. Preserve `dev` / `qamc` / `ubuntu` isolation and existing secret-handling conventions. Claude must stop for operator action when an actual secret or privileged runtime change is required and must not merge its own work.

### Runtime commissioning accepted

The final privileged `qamc` live verifier run against current `main` passed on 2026-08-14:

- **37 PASS / 0 FAIL / 0 WARN / 1 SKIP**
- `COMMISSIONING ACCEPTANCE: PASS`
- process exit `0`
- config/routing validation PASS
- OneCLI gateway/wiring/provider injection PASS
- Mission Control DB/paper/broker health PASS
- OpenRouter completions for both accepted policy models PASS
- Alpaca Paper account/data/quote/calendar PASS
- FRED PASS
- trading timers disabled PASS before activation
- no committed secrets PASS
- Mission Control read-only PASS

The remaining `qamc` SKIP is the intentionally off-account isolation check. The already-green `dev` commissioning run proves that boundary. **Full commissioning acceptance is the union of those two green account runs and is complete.**

### Alpaca Paper soak activated

On 2026-08-14 the operator activated all seven existing `qamc` user timers after commissioning acceptance:

- `quant-agent-morning.timer`
- `quant-agent-midday.timer`
- `quant-agent-intra_check.timer`
- `quant-agent-close.timer`
- `quant-agent-evening.timer`
- `quant-agent-earnings_preprocess.timer`
- `quant-agent-daily.timer`

Systemd reported all seven as `enabled`. The six trading-stage timers are scheduled every 30 minutes and self-gate to their intended ET windows. Their first scheduled post-activation tick was **2026-08-14 18:30 UTC / 14:30 ET**. The daily P&L CSV export is scheduled for **Mon–Fri 09:00 America/New_York**.

This marks the start of the authorized **Alpaca Paper soak**. It does **not** authorize live-money trading.

## Private operator access

The OVH host's private operator path was completed and verified on 2026-08-14:

- Tailscale is authenticated, enabled and healthy.
- The **tailnet DNS name** is `wallaby-bowfin.ts.net`.
- The OVH VPS **Tailscale machine name is `ovh-vps`**, verified from live `tailscale status` on 2026-08-18.
- Therefore the VPS's canonical MagicDNS FQDN is **`ovh-vps.wallaby-bowfin.ts.net`**. Use that FQDN for explicit SSH/HTTPS configuration; the shorter `ovh-vps` may also resolve on clients where MagicDNS search domains are active.
- Older `redstone-vps` references are obsolete and must not be used for current Tailscale access.
- `wallaby-bowfin.ts.net` by itself names the tailnet DNS namespace, not the VPS node, and must not be used as the SSH host.
- Tailscale Serve provides tailnet-only HTTPS to Mission Control while the API remains bound to `127.0.0.1:8800`; ports 443 and 8800 are not publicly reachable.
- Existing OpenSSH key authentication for `ubuntu` and `dev` was verified over the tailnet. Tailscale SSH remains disabled so the existing key-based OpenSSH path stays authoritative.
- Public port 22 remains temporarily available as the recovery path until the operator validates SSH from the other regular Tailscale clients; it must not be removed before that validation.
- Orca development tooling runs under `dev` from `/home/dev/projects`; host-administration work is isolated under `ubuntu` in `/home/ubuntu/orca-admin`; `qamc` remains runtime-only.
- Only `ubuntu` has sudo administration. `dev` and `qamc` have neither sudo nor Docker-group membership, and no foreign-owned files were found in the three account homes.
- OneCLI remains healthy and loopback-only on ports 10254/10255.

## Accepted model policy

- OpenRouter remains the model-provider path.
- Eight seats run `google/gemini-2.5-flash-lite`.
- `risk_manager` runs `qwen/qwen3-235b-a22b-2507` for decision-chain independence at measured-equal RM quality.
- Three seats (`earnings_analyst`, `evening_analyst`, `meta_reflector`) remain assigned by analogy rather than direct seat measurement; this is a known limitation.
- Projected LLM spend is approximately **$72.10 → $1.14/month** under the measured workload assumptions.
- Full contract: `docs/architecture/MODEL_ROUTING_POLICY.md`.

## Accepted decision-chain audit

- Risk Manager receives position-age/drawdown evidence needed to audit its rules.
- RM reads primary evidence before PM narrative; PM claims are not treated as primary evidence.
- Missing PM audit steps are observable through explicit rendering and a non-blocking advisory.
- Unsourced valuation claims are detected/logged rather than contaminating cached filing evidence.
- Inherited Apr–Jul 2026 behavioural priors retain provenance and lose precedence to current-account evidence once available.
- **No deterministic risk or execution semantics changed.** Alpaca remains Paper-only.
- Full record: `docs/architecture/DECISION_CHAIN_AUDIT.md`.

## Paper-soak findings now accepted as work-driving evidence

The first operator review of real soak behaviour and Mission Control on 2026-08-18 exposed issues that are now legitimate post-start work rather than speculative polish:

- **SGOV is deterministic cash parking, not a Portfolio Manager investment thesis.** The cash-sweep subsystem parks excess raw cash in SGOV, hides it from LLM-facing portfolio views, treats it as cash-equivalent for risk, and releases it before real BUYs. Mission Control currently presents SGOV like an ordinary position, which can materially mislead the operator about risk posture and deployable liquidity.
- **QAMC has an existing bearish path but is not currently a direct-short system.** `SH`, `SDS`, `PSQ` and `SQQQ` are already in the trading universe and deterministic risk code handles their signed inverse exposure and leverage. The Portfolio Manager cannot emit negative target weights; SELLs reduce/close owned positions. Direct stock shorting, options/theta strategies and margin are therefore outside the current implementation and remain unauthorized.
- **The product must not be structurally long-only.** Within the currently supported instrument set, QAMC is expected to consider and exploit credible bearish opportunities as well as bullish ones. This is now explicit in `docs/OUTCOME.md`. It does not require a trade every down day and does not authorize hindsight-driven chasing.
- **A possible long/caution bias requires evidence-based audit.** The PM and Evening prompts are intentionally swing/position oriented and still contain inherited Apr–Jul 2026 priors aimed at correcting predecessor-account under-investment and missed leaders. Those priors are provenance-tagged, but the running account has little history. The Aug 17–18 decline therefore warrants a forensic check of whether bearish/inverse-ETF evidence reached Tech → PM → RM correctly before changing intelligence.

  **Forensic outcome (2026-08-19, one-pass reconstruction against the live `qamc` runtime via the read-only Mission Control API):** no genuine blind spot found; no correction made. Sampled six of the eighteen 2026-08-17/18 scheduled runs (open/mid/close on each day) plus a full-window `/search` sweep of every `tech_analyst` universe-scan log line. `tech_analyst` rated `SQQQ` (the -3x Nasdaq inverse ETF already in the universe) `sell` or `neutral` on every sampled run — never `buy` — with reasoning tied to `SQQQ`'s own bearish trend/momentum plus an explicit volume-confirmation caution; `SPY`/`IWM`/`DIA`/`XLE`/`XLF` were themselves rated `buy`/`neutral` throughout both days, i.e. the system's own technical evidence read bullish-to-neutral on the broad market, not bearish. `macro_analyst` reported regime `transitional`, equity outlook `neutral`, confidence `low`, explicitly citing missing VIX data and stale inflation figures — an honestly-disclosed data-freshness gap, not a suppressed or misrepresented signal. `news_analyst` sentiment was `neutral` (Middle East tensions / rising yields as headwinds, not a decline narrative). Because `tech_analyst` never emitted a `buy` rating on `SQQQ`, `portfolio_manager` never received a target/proposed-order for it on any sampled run — the PM stage was never reached because no qualified bearish setup existed upstream, not because evidence was dropped or discounted. The account was flat both days (+$0.98 and +$1.96 on ~$10k, zero positions, zero trades) and both evening reflections recorded an explicit, coherent rationale ("cautious stance amidst significant geopolitical and macroeconomic headwinds") with empty `missed_opportunities_json`. Classification: **no qualified bearish setup existed** — a legitimate, evidence-consistent neutral/cash outcome, not a directional or prompt-level blind spot. Per the "do not hindsight-fit" instruction, no intelligence/prompt correction was made; Workstream A closes as investigated-and-clean rather than investigated-and-corrected.
- **Mission Control buries the useful explanation.** Existing API/UI code already records per-candidate specialist evidence, PM reasoning/targets, RM verdict/modifications, deterministic gate records and execution. The current top-level dashboard still makes the operator drill into run/candidate modals to answer the basic question “why did QAMC do nothing?” A 2026-08-18 morning run visibly considered candidates while producing no decision, making this an observed usability gap.
- **Missed-opportunity data exists but is not prominent enough.** The Evening Analyst can review notable UP or DOWN moves and the journal can render `missed_opportunities`; the operator should not need to infer from an empty trade table whether the system recognized a significant move.

## Mission Control cockpit rebuild (Stage 6) — implemented and cut over to production (2026-08-19)

Branch `claude/mission-control-cockpit-redesign` implements the final-cockpit
tranche `docs/WORK.md` authorized (superseding the prior generic
framework-migration prohibition for this bounded case):

- Backend: `/account.liquidity` (raw cash / sweep-parked / reserve /
  deployable), `/positions[].direction`
  (`long`/`bearish_hedge`/`cash_equivalent`), `GET /runs/{id}/funnel`
  (structural decision funnel + quoted PM/RM/macro context), and
  `GET /prices/{symbol}` (daily OHLCV, Alpaca's market-data client) — all
  bounded, read-only, GET-only, isolation-tested. Full suite: 1857
  passed, 0 failed.
- Frontend: evaluated the vanilla-JS prototype built earlier this
  tranche (preserved at commit `73c68bf`) against React+Vite+Tailwind+
  TradingView Lightweight Charts per `docs/WORK.md`'s explicit direction
  to evaluate that stack first. The prototype proved the information
  architecture but could not economically deliver real charting, a true
  multi-pane layout, or Orallexa-style consensus visualization without
  hand-building equivalent infrastructure — so the cockpit was rebuilt in
  React (source in `frontend/`, compiled output replacing
  `src/api/static_cockpit/`, same `/cockpit` static mount, same
  no-runtime-dependency deployment model as `/ui`).
- `/ui` (Stage 3-5 dashboard) remains deployed alongside `/cockpit` as the
  operational fallback per the parallel-build/cutover rule. Production
  cutover completed via PR #46 on 2026-08-19; both `/cockpit` and `/ui`
  are confirmed healthy on production `main` at `7668771`.
- Browser-verified desktop + iPad + dark mode against seeded
  representative data: `docs/verification/stage-6-react/`.
- The Aug 17–18 directionality forensic (Workstream A) is closed:
  investigated, no blind spot found, no correction made — see the
  "Paper-soak findings" section above for the full evidence trail.

### Stage 6b — agent deliberation, journal narrative, directional bias

A follow-on same-branch tranche, orchestrated as three parallel isolated-
worktree workstreams then integrated and visually polished by the lead:

- **Agent deliberation UX**: `SpecialistCards` (one card per specialist
  that actually produced evidence, with identity, direction, conviction,
  reasoning, an honestly-derived aligned/diverges indicator) replaces the
  old flat consensus list; a reusable `DecisionFlowDiagram` (Specialists →
  PM → AI Risk → Deterministic Gate → Execution) is used both in the
  candidate drill-down and, aggregated, in the main funnel panel.
- **Decision-process observability**: `reason_category`, the full RM/PM
  reasoning-chain breakdowns, PM's continuity/premortem disclosure flags,
  and a derived clean/modified/rejected state are now surfaced — all data
  that was already flowing through the API, previously untyped/unrendered.
- **Journal rebuilt into a real day-by-day narrative**: morning regime,
  per-run decision cards (candidates with direction tags, PM/RM text,
  explicit trade/no-trade), full evening-reflection fields, prev/next
  date navigation.
- **Directional-bias observability panel**: bullish/bearish/neutral
  candidate and proposal counts, inverse-ETF consideration, AI Risk
  approve/reject breakdown by direction (explicitly caveated as run-level
  not per-candidate), and a dominant-outcome histogram — aggregated
  client-side from existing endpoints, no new backend surface, explicitly
  framed as observability only ("not a trading signal or recommendation").
- **Visual QA pass**: increased panel contrast/depth (the flat/monochrome
  weakness was real), agent-identity badges, and two genuine responsive
  bugs found and fixed (a `sm:`-breakpoint 4-column grid squeezed inside
  a half-width panel at iPad width; the top stat strip hid 4 of 5 stats
  behind an undiscoverable horizontal scroll).

Evidence: `docs/verification/stage-6b-deliberation-journal-bias/`. Full
backend suite unaffected throughout (1857 passed, 0 failed) — this
tranche is frontend-only.

### Stage 6c — directional exposure semantics fix (external review finding)

`CandidateFunnelItem.direction` is the instrument's own signal, not
resulting portfolio exposure — for an approved inverse ETF, a bullish
instrument signal expresses bearish market exposure. The Directional Bias
panel was conflating the two. Fixed: `exposureDirection()` flips
bullish/bearish only when `is_bearish_hedge` is true (derived from that
API flag, never a symbol heuristic); candidate and PM-proposal direction
aggregation now show both series, clearly labeled. 8 new Vitest tests
(dev-only dependency, confirmed absent from the production bundle).
Evidence: `docs/verification/stage-6c-directional-exposure-fix/`.

### Stage 6d — operator branch-preview endpoint

`ops/preview/branch_preview.py`: an ephemeral (no systemd unit, no
auto-start), GET-only, `dev`-account process letting the operator review
this branch's actual `/cockpit` and `/ui` frontend from any tailnet
device before merge, proxying real (never faked/duplicated) `qamc`
production data via loopback GET requests to `127.0.0.1:8800` — the only
data source available without weakening the `dev`/`qamc` account
boundary. Binds only to the VPS's Tailscale IP (`100.111.170.97` /
`ovh-vps.wallaby-bowfin.ts.net:8810`) — verified structurally
unreachable via the public IP and even `127.0.0.1`. Verified `qamc`'s
Mission Control process untouched (identical PID/uptime/health before
and after). Documented known limitation: panels depending on this
branch's not-yet-deployed backend endpoints degrade honestly rather than
faking data. Evidence: `docs/verification/stage-6d-branch-preview/`.

### Stage 6e — branch-preview API contract fix (real data, end-to-end)

Fixed the blocker recorded in Stage 6d: old `qamc` production predates
this branch's Stage 6 backend additions (`AccountResponse.liquidity`,
`PositionItem.direction`/`is_cash_equivalent`, `GET /runs/{id}/funnel`),
so the decision funnel, directional bias, liquidity, and position-
direction panels rendered blank against the proxy. `ops/preview/
branch_preview.py` now reconstructs exactly those missing fields/routes
from data upstream **already** exposes (`/account`, `/positions`,
`/runs/{id}`, `/runs/{id}/candidates(/{symbol})`, all already deployed),
using the same typed schemas and pure derivation rules this branch's own
backend uses — never a direct DB/broker-credential read from the `dev`
account, and self-obsoleting once production actually deploys this
branch. `GET /prices/{symbol}` (the one field needing real Alpaca
market-data credentials `dev` doesn't have) continues to degrade
honestly, as before.

Verified interactively in a real connected browser (zero failed requests,
zero console errors) and independently with a local headless Playwright
script (zero console errors across desktop/iPad/legacy-`/ui`) against the
live preview serving real `qamc` production data — Decision Room funnel,
Directional Bias, Cash & Risk Exposure, positions direction, run detail
(81 real candidates), candidate drill-down, and the day-narrative Journal
all confirmed populated with genuine account/run data, not fabricated.
`qamc` production `/health` confirmed identical before/after (untouched,
never restarted). Full backend suite: 1857 passed, 0 failed (unchanged).
Frontend Vitest: 8 passed (unchanged — no `frontend/`/`src/api/` files
were touched this tranche). Evidence:
`docs/verification/stage-6e-preview-contract-fix/`.

### Stage 6f — cockpit information architecture + visual redesign

Operator review of the Stage 6e cockpit found it still read as a long
telemetry/admin page (a flat stacked two-column layout, including an
unbounded ~80-ticker flat pill dump) rather than a compact Mission
Control cockpit. Frontend-only restructure, no backend/API changes:

- `App.tsx` rebuilt into a real cockpit grid — candidate rail (left) /
  chart + selected-symbol context (center) / Decision Room (right) at
  desktop width, collapsing to an explicit `Candidates / Chart /
  Decision Room` tab strip below the `xl` (1280px) breakpoint (covers
  every iPad size) instead of a squeezed 3-column layout. Nine
  previously-stacked full-width panels (positions, orders, trades, runs,
  directional bias, missed opportunities, search, health) are now a
  single tabbed support area below the cockpit. Cockpit vs Journal is
  its own top-level view switch.
- `CandidateRail` (replaces `WatchlistPanel`): the candidate universe is
  bucketed by the furthest funnel stage each candidate reached
  (rejected by specialist / reached PM / proposed / modified-or-blocked
  by risk / executed), with clickable filter chips, a funnel-bars
  visualization, a symbol filter, and a height-bounded scrollable list
  — no more unbounded flat pill dump.
- `DecisionRoomPanel` (replaces the old full-width decision-funnel
  panel): a narrow-column Specialists → PM → AI Risk → Deterministic
  Gate → Execution chain, rendered **vertically** so all 5 stages are
  visible without scrolling, with the deterministic gate visually
  marked "Final authority" (thicker border, explicit label) rather than
  reading as just another agent step.
- Real visual toolbox added where the cockpit was plain text/tables:
  conviction meter bars on specialist cards, segmented exposure/
  liquidity bars on the cash & risk panel, an always-visible header
  exposure gauge (`ExposureStrip`, real long/hedge/cash split from
  already-fetched account+positions data), a real-data equity sparkline
  on the Journal view.
- Two real bugs found and fixed during this same pass: a CSS Grid `1fr`
  track without `min-width:0` let the price chart's content width push
  the Decision Room column off-screen; `lightweight-charts`' `autoSize`
  resized the chart canvas correctly when its pane went from hidden to
  visible but never re-fit the visible time range, rendering bars
  compressed into a sliver — replaced with an explicit `ResizeObserver`
  that resizes and fits together.
- Verified against real `qamc` production data (honest current empty
  state — zero candidates, zero non-cash positions) via the existing
  `ops/preview/branch_preview.py`, and against a throwaway seeded
  scenario (not committed) spanning every candidate-funnel bucket, both
  desktop and iPad, zero console/page errors across 12 scripted
  Playwright captures. `qamc` production `/health` confirmed unaffected
  (same PID, untouched). `npm run build` + `npm run test` (8/8) pass.
  Evidence: `docs/verification/stage-6f-cockpit-ia/`.

Merged to `main` via PR #46 and cut over to production on 2026-08-19 —
see the Stage 6 header above. `/cockpit` and `/ui` are both mounted and
confirmed healthy on production `main` at `7668771`.

### Stage 6g — cockpit visual identity system + stale-data correctness fix

A real Stage 6f screenshot the operator captured exposed two problems Stage
6f's information-architecture fix didn't reach: the cockpit still read as a
generic dark admin panel (weak typography, plain bordered cards, a
Decision Room that was five stacked rectangles), and a genuine correctness
bug — a failed API poll could leave old `funnel`/`positions`/`account` data
rendering as if current, underneath a separate visible error message
elsewhere on the same page. This tranche fixed both, after a dedicated
research pass (external component-ecosystem and product-pattern research,
including a public MIT React+Vite AI-trading-frontend project
(`sh1ftmaker/helm`) read directly for its decision-card visual grammar) the
operator reviewed and explicitly authorized a small, coherent set of mature
visualization libraries for — deliberately **not** minimizing dependency
count, on the operator's explicit instruction that best-in-class UX
outweighs dependency-count minimization for this product:

- **Design tokens**: a color grammar where hue carries meaning (cyan =
  system/brand, green/red = market truth only, violet = AI reasoning,
  amber = attention, magenta = bearish-hedge flag), IBM Plex Sans/Mono
  typography, a subtle vignette/dot-grid background replacing flat black.
- **Hero band** + a full-width **decision-state banner** (EXECUTED / NO
  TRADE / REJECTED / DETERMINISTIC GATE BLOCKED) promoted above the
  primary cockpit body — WORK.md's "what do I own / what's the market
  doing / why did it (not) trade" questions are now answered before any
  interaction, not just after opening the Decision Room.
- **Decision Room rebuilt on React Flow**: the Specialists → PM → AI Risk →
  Deterministic Gate → Execution chain is a real node/edge graph with
  genuine per-specialist fan-in (one node per specialist that actually
  produced evidence, not a flattened "Specialists" box), and the
  Deterministic Gate is a categorically different node **shape** (a
  hexagonal hard-interlock outline with a hazard-stripe fill), not just a
  thicker border — the single most-requested fix ("not five prettier
  rectangles").
- **CandidateRail** on TanStack Table (real column sort + expandable rows)
  and an ECharts native funnel series; real BUY/SELL trade markers wired
  onto the price chart via `lightweight-charts`' existing (previously
  unused) marker API.
- **Desktop-only Dockview support workspace** — explicit, scoped operator
  approval for this one new architectural dependency: resizable/
  draggable/poppable, default layout matching the old tab order, never
  wraps the primary cockpit, never instantiated below the `xl` (iPad)
  breakpoint, layout persists to `localStorage` only (non-authoritative).
- **Stale-data correctness fix**: every polled resource now tracks data +
  error + last-good timestamp separately; a failed poll never overwrites
  good data, and every affected panel renders an explicit "STALE — last
  known data as of HH:MM" state instead of silently continuing to show old
  data as current. Verified with a real forced-failure → recovery cycle
  (Playwright route-blocking), not a mock.

Frontend-only (`git diff --stat` confirms zero changes under `src/` outside
the pre-existing compiled `src/api/static_cockpit/` bundle); `/ui` and real
`qamc` production confirmed untouched. Full details, the library-by-library
authorization record, and the stale→recovery screenshots:
`docs/verification/stage-6g-cockpit-visual-system/`.

Deferred to a follow-on tranche (scoped but not implemented this pass): the
ECharts Sankey candidate-branching detail view and the missed-opportunities
scatter chart, both explicitly secondary/detail-view items in the design
plan.

### Stage 6h — mature-visualization upgrade + chart dead-space fix

A follow-on external review of a real Stage 6g screenshot found the
information architecture and visual-identity work sound but flagged three
concrete defects and asked for a bounded, evidence-based research pass
(information → best visualization pattern → best available mature
component → KEEP/TRANSFORM/REPLACE/CONSOLIDATE/REMOVE) rather than another
prettifying pass over homemade primitives:

- **Chart dead-space fix**: the cockpit's three-column row now shares one
  explicit viewport-bounded height (`xl:h-[calc(100vh-150px)]`) instead of
  an unconstrained grid cell; the candidate rail and Decision Room scroll
  independently within it, and the chart's `flex-1 min-h-0` wrapper lets it
  fill whatever vertical space is actually available (240px floor) instead
  of the old hard-coded 260px that left a large dead region below it on
  any taller viewport. `PriceChartPanel`'s `ResizeObserver` now reads
  height as well as width. Verified at two desktop heights (1000px,
  760px) — no dead region at either.
- **Liquidity + Real-Risk-Exposure donuts** (`DonutMeter.tsx`, ECharts
  `pie` in donut mode, sharing `ArcGauge`'s token/substrate) replace both
  `SegmentedBar` thin bars the operator had already rejected once. SGOV
  sweep-parking keeps a distinct, truthful `dim` category — never
  market-status green/red, never the brand-accent cyan.
- **Positions treemap** (`PositionsTreemap.tsx`, new): block area = market
  value (concentration), block color = real unrealized P&L sign — a
  treemap rather than another donut because holdings are a real,
  unbucketed hierarchy, not a 2–3-category composition.
- **Missed-opportunities scatter**: `MissedOpportunitiesPanel` now
  aggregates the last 20 journal days (existing endpoints, no new backend
  surface) into an ECharts scatter — date × signed move % × genuine-miss/
  disciplined-pass coloring — so the up/down miss balance `docs/
  OUTCOME.md` cares about is visible longitudinally instead of inferred
  from one day's text. Previously deferred pending a real case for it;
  this is that case.
- **Decision Room re-assessed against the review's explicit 9-question
  bar and found already sufficient** (Stage 6g's React Flow graph +
  existing `CandidateDetailModal` drill-down) — deliberately **not**
  rebuilt, to avoid the "prettier homemade primitive" failure mode the
  review warned against. Sankey reassessed and still deferred: current
  candidate volumes don't yet make the case for it over the existing
  funnel bars + table.
- **Two real bugs found and fixed**: the in-flight (uncommitted at pass
  start) `DonutMeter` work was typecheck-broken (`GaugeTone` missing the
  just-added `"hedge"` member; two ECharts `graphic` text elements used
  the nonexistent `textAlign` instead of `align`) and, once compiling,
  `LiquidityPanel` had been silently re-pointed to `tone: "neg"` (market-
  loss red) for bearish-hedge and `tone: "accent"` (brand cyan) for
  cash-equivalent — both violate this project's own color-grammar
  contract. Fixed by extending `DonutMeter`'s tone set and restoring the
  correct semantic tones. Separately, this pass's own verification
  fixture had an invalid `RiskVerdict.reason_category` enum value and a
  `RiskModification` missing its required `symbol` field, which Pydantic
  silently degraded to a missing verdict (confirming, not breaking, the
  production degrade-don't-crash contract) — fixed in the fixture.

Frontend-only; `git diff --stat -- src/` confirms zero backend/trading
changes beyond the compiled `src/api/static_cockpit/` bundle, and `/ui` is
byte-for-byte untouched. Verified with a seeded throwaway fixture spanning
six days and every decision state (same monkeypatch-at-the-broker/db-read-
seam convention as Stage 6f/6g), captured via a local headless Playwright
script at two desktop heights and iPad width, zero console/page errors
across every scenario. `npm run build`/`npm test` and the backend safety/
isolation test subset (`test_api_safety.py`, `test_api_isolation.py`,
`test_api_no_secrets.py`, 41 tests) all pass. Full details, the color-
grammar bug writeup, and screenshots:
`docs/verification/stage-6h-visualization-upgrade/`.

Merged to `main` via PR #46 alongside Stage 6f/6g and cut over to
production on 2026-08-19 — see the Stage 6 header above.

## Three-defect forensic fix (2026-08-19) — merged to main; production deployment pending

A read-only production forensic on 2026-08-19 established three defects.
PR #48 (`fix/sgov-liquidity-intraday-batch`) was externally reviewed and
merged to `main` on 2026-08-19 as
`70099c15097b77e6194a4cae247a9bacbea9a201`. **Production has not yet
been deployed from this merge.** Full suite: 1907 passed, 0 failed
(baseline 1857). The new intraday scan remains disabled by default pending
explicit rollout.

1. **SGOV funding semantics.** PM/RM/the deterministic gate were shown
   ~$10K "cash" while real cash was ~$145; SGOV was sold to fund approved
   BUYs but execution's recheck found no money, so every BUY was skipped.

   **Corrected after external review.** A first pass mis-diagnosed this as
   T+1 settlement and switched sizing to
   `non_marginable_buying_power` — the wrong field. Verified against
   Alpaca's official documentation: `cash` is credited **as soon as a SELL
   fills** ("the cash is updated post the SELL trade is filled, but the
   cash_withdrawable and cash_transferable are updated post T+1"), so a
   *filled* SGOV liquidation genuinely funds an equity BUY the same
   session. `non_marginable_buying_power` is the settled/crypto figure and
   *lags* a same-day equity sale by a business day; `buying_power` /
   `regt_buying_power` are margin figures (~2x equity — every Alpaca
   account is a margin account) and must never be used.

   Final implementation: `_compute_deployable_cash()` = raw `cash` + the
   convertible sweep value — both assets already owned, so it can never
   exceed equity or imply leverage. The real defect was never the
   crediting; it was *assuming the sale filled*. `CashSweeper.fund_buys()`
   now reports the **confirmed** rise in raw broker cash instead of the
   notional it submitted, and fails closed (reports $0, leaves cash
   untouched) when it cannot confirm. `reserve_pct` stays at **1.0** — the
   first pass's raise to 5.0 treated a symptom and is reverted.
   Execution's final raw-cash recheck is **unchanged and authoritative**.
2. **Intraday opportunity-discovery blind spot.** Opportunity generation
   ran once each morning; `intra_check` was loss-protection only. Added a
   bounded scan on the **existing** `intra_check` cadence — **no new
   timer/service/daemon**: one bulk Alpaca snapshot call flags symbols
   that moved past a threshold since the last close; only those (capped,
   cooldown-deduped) get a real `tech_analyst` call and then the **same**
   DecisionStage → RiskStage → ExecutionStage chain morning uses.

   **Intraday evidence corrected after external review:** the scan
   detected candidates on live prices but then handed Tech only completed
   daily bars ending at the prior close, so the very move that triggered
   the scan was invisible to the analyst judging it. The snapshot now also
   carries today's still-forming session bar, and Tech renders it as an
   explicit `CURRENT SESSION (TODAY, INCOMPLETE)` block — live price, move
   vs prior close, session O/H/L and partial-day volume — kept out of the
   completed-bar series, with the indicators explicitly flagged as
   predating the move. An incomplete day is never shown as a finished
   daily bar; the old mislabelled "Current close" is now "Last completed
   close". Bearish
   views surface through the already-approved inverse ETFs
   (`SH`/`SDS`/`PSQ`/`SQQQ`) — no shorting, options or margin added.
   Ships **disabled** (`intraday_scan.enabled: false`) pending explicit production rollout. Macro/news/earnings are deliberately not re-run and are marked
   `not_run_intraday`, so RiskStage's existing degraded-data advisory
   fires honestly and RM knows the evidence is thinner than a morning run.
3. **Tech batch-response symbol loss.** Symbols sent to `tech_analyst`
   silently vanished during batch parsing (one chunk parsed 1/10).
   `analyze_batch` now guarantees every submitted symbol is a key in the
   returned dict — a result on success, or `None` for a visible terminal
   failure after one bounded retry that re-asks only the missing symbols.
   Retry token/cost/latency is merged, never dropped. Callers filter
   `None` and surface a `partial` data status instead of a silent "ok".

Two concurrency defects in the new intraday path were found by verifying
the scheduling assumptions against `scripts/run_if_et_window.sh` rather
than trusting them: that script exempts `intra_check` from the cross-mode
session lock *because its actions are idempotent*, which opening a new
position is not. Both fixed — a fail-closed 15-minute DB-row guard
against racing another session, and an advisory `flock` mutex against two
overlapping `intra_check` processes.

## Current product priority: directionality + explainability during the live paper soak

The paper soak continues uninterrupted. The next authorized tranche is to use actual Aug 17–18 evidence to determine whether the system's lack of risk deployment was intentional, a candidate-generation/agent bias, a risk veto, or simply no qualified setup, while simultaneously fixing the dashboard presentation defects that make that distinction difficult.

Priority order:

1. forensic reconstruction of bearish opportunities through specialist → PM → RM → deterministic gate → execution;
2. make raw cash, SGOV sweep liquidity, real risk exposure and deployable liquidity visually distinct;
3. put a latest-run decision funnel / “why no trade?” explanation on the main Mission Control view;
4. surface directional posture, inverse-ETF consideration and missed opportunities using existing read-only evidence where practical;
5. change prompt/agent behaviour only if the forensic evidence demonstrates a real directional blind spot.

## ChatGPT GitHub integration role

ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly for supported repository reads/writes, PR creation/review, merges and routine administration. Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform.

## Not authorized without a new contract

- Any live-broker trading.
- Direct stock shorting, options trading/theta strategies, or enabling margin.
- Any second model provider or silent fallback model.
- Deterministic risk/execution semantic redesign.
- Broker-write Mission Control controls.
- Public exposure of QAMC or OneCLI.
- Collapsing `dev` / `qamc` / `ubuntu` account boundaries.
- Replacing upstream OneCLI or adding a new durable routing platform without a new architectural decision.

## Handoff

Paper soak remains active. Execute the authorized Telegram notification restoration tranche in `docs/WORK.md`. The PR #48 production deployment and intraday rollout remain separate, pending work and must not be touched as part of Telegram restoration.
