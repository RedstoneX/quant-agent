# Stage 6j — agent cards, disagreement/signal fusion, and truth convergence

Second convergence pass in this branch, on top of Stage 6i, following the
operator's explicit direction that Stage 6i was "a strong first pass" but
not yet complete Dashboard convergence: materially realize the vision
board's Oralexa-style specialist agent cards, disagreement/signal-fusion
view, and compact PM → Risk → deterministic gate → execution story on the
**primary** cockpit screen (not only behind a drill-down modal), extend
the same real specialist/consensus computation into the Journal Day
narrative as explicit Agent Analysis / Disagreements sections, and resolve
the remaining "invented pseudo-confidence" / "arbitrary UI risk threshold"
truth-debt items from `docs/WORK.md`.

Rehydrated against the latest `docs/mission-control-product-plan`
governance updates before starting (merged into this branch — see the
merge commit immediately preceding this stage's work: the Mission Control
vs. Dashboard terminology split, no scope/boundary change).

**Captured against:** commit `94b5abad7f6d9f1f6b9e9ea16d87689acf351d58`
(this stage's work committed together with this evidence, per this
project's established convention).

**Verification date:** 2026-08-21, ~00:00–00:25 UTC.

## What changed

### 1. Oralexa-style agent cards + disagreement/signal fusion, on the primary screen

`DecisionRoomPanel` (the cockpit's right-column "how it got there" panel)
previously always rendered the run-level 5-node aggregate graph
(`buildRunGraph` — "95 considered", no specialist detail). The real
per-specialist fan-in graph (`buildCandidateGraph` — one card per
specialist with direction, conviction, reasoning excerpt, and a genuine
`consensus.agreement`-derived "Aligned with consensus" / "Diverges from
majority" / sole-view badge) already existed, built in an earlier stage,
but was reachable only by clicking into `CandidateDetailModal`.

`DecisionRoomPanel` now fetches `CandidateDetailResponse` for whichever
symbol is currently charted and renders the same real per-specialist graph
directly on the primary screen — the panel title becomes
`Decision Room — {SYMBOL}`, falling back to the run-level aggregate only
when no candidate is selected or the fetch hasn't resolved. A real layout
bug was found and fixed along the way: the existing horizontal layout
(sized for the wide modal) clipped/crushed illegibly in the ~340px cockpit
rail — `buildCandidateGraph` gained a `"vertical"` layout mode (specialists
stacked, then the PM→Risk→Gate→Execution chain continues below), reusing
the exact same node/edge logic, never a second graph implementation.

`buildCandidateStages` (candidate-level stage derivation) moved from
`CandidateDetailModal.tsx` to `funnelShared.tsx` so both surfaces share one
source of truth rather than risking two implementations drifting apart.

Run-wide PM/Risk cards that remain below the graph are now explicitly
labeled "— run-wide" so they can never read as if derived for the panel's
titled symbol — directly addresses `docs/WORK.md`'s "Candidate Detail can
misattribute run-wide PM/Risk evidence to an individual ticker" debt item
for this new surface.

### 2. Agent Analysis & Disagreements in the Journal Day narrative

`docs/OUTCOME.md`'s Journal Day sections 3–4 ("Agent Analysis",
"Disagreements") were not yet distinct sections — PM/Risk narrative text
existed per run, but no cross-specialist view. `JournalPanel` now computes
the day's "notable" candidates (reached the Portfolio Manager in any run
that day — the majority "screened, no PM target" bucket has no PM-relevant
disagreement to report), fetches their `CandidateDetailResponse` in
parallel (the same bounded-fan-out pattern already used for per-run
funnels), and renders each with its real specialist cards + the backend's
own consensus badge, plus a callout listing any genuine `"mixed"`
disagreements. Reuses `buildEntries` (exported from `agentflow/
buildGraph.ts`) rather than a second specialist-listing implementation.

### 3. Invented pseudo-confidence percentages removed

Found two identical instances of the exact anti-pattern
`docs/OUTCOME.md`'s agent-card principle names — a qualitative high/medium/
low field mapped to a fabricated precise-looking percentage
(`{high: 92, medium: 58, low: 28}`) purely to size a continuous progress-
bar fill, in `agentflow/nodes.tsx` (specialist conviction) and
`HeroBand.tsx` (macro-regime confidence) — both claiming a measurement
the model never produced. `ui/Meter.tsx` gained `LevelBar`, a discrete
3-segment level indicator (filled-segment count, never a percentage width)
reused in both places and in the new Journal Agent Analysis cards.

### 4. Arbitrary UI risk threshold tied to the real deterministic limit

The hero band's "Risk deployed" gauge used fixed 40%/75% color-band
splits with no relationship to any configured limit. QAMC's deterministic
risk gate has a real, authoritative ceiling
(`config/settings.yaml`'s `risk.max_total_position_pct: 90`,
`src/risk/rules.py`'s actual hard-block rule) that was not previously
exposed via the read-only API at all. Added the minimum read-only
contract per the operator's explicit instruction ("if the UI genuinely
lacks required data, add only the minimum read-only contract needed"):

- `src/api/deps.py::get_risk_limits()` — mirrors the existing
  `get_cash_sweep_*` accessor pattern exactly.
- `src/api/schemas.py::RiskLimits` — percentages only, no secrets; `None`
  on any config-read failure, never a guessed default.
- `src/api/routes_live.py::_compute_risk_limits()` /
  `AccountResponse.risk_limits` — same fail-closed-to-empty posture as
  the existing `_compute_liquidity()`.
- `HeroBand.tsx`'s gauge bands now scale against the real
  `max_total_position_pct` when available; when absent (e.g. old
  production, verified in this pass), it degrades to a single neutral
  tone rather than silently reverting to the old arbitrary split.

Zero change to `src/risk/rules.py` or any deterministic risk/execution
logic — this is a read-only display value, never fed back into any
decision path (`.claude/rules/trading-core.md` respected; only
`src/api/` and this branch's own `ops/preview/branch_preview.py` dev-only
verification tool were touched).

## Screenshots

| # | File | Viewport | Scenario |
|---|---|---|---|
| 01 | `01-desktop-decision-room-agent-cards-nvda.png` | Desktop 1600×1000 | NVDA candidate, primary cockpit: Technical Analyst card (direction/conviction/reasoning excerpt) → Portfolio Manager (REACHED, Target 8%) → AI Risk Manager (REJECTED, "rr fail") → Deterministic Gate (hexagon, NOT REACHED) → Execution — all real, all inline, no modal click needed. |
| 02 | `02-desktop-decision-room-full-chain-aapl.png` | Element screenshot (full scrollable height) | AAPL: Technical Analyst + Earnings Analyst, both "Aligned with consensus" (real backend-computed badge) → full chain → "Portfolio Manager — run-wide" / "AI Risk Manager — run-wide" cards, explicitly labeled to prevent misattribution. |
| 03 | `03-ipad-decision-room-agent-cards.png` | iPad 820×1100 | Same real per-candidate agent graph at iPad width, reached via the Decision Room tab — no layout breakage. |
| 04 | `04-desktop-cockpit-real-risk-limits-levelbar.png` | Desktop 1600×1000 | Hero band gauge showing "vs. 90% deterministic ceiling" (real config value, via a locally-extended `branch_preview.py` proxy — see Method) and the corrected discrete `LevelBar` confidence indicators (specialist cards + Market Regime), replacing the fabricated percentage bars. |
| 05 | `05-ipad-cockpit-graceful-degrade-no-risk-limits.png` | iPad 820×1100 | Same gauge against **real, unmodified production** (no `risk_limits` field yet) — degrades to a single neutral tone rather than silently falling back to a fabricated threshold. |
| 06 | `06-desktop-journal-agent-analysis-disagreements.png` | Desktop 1600×2913 (full page) | Real trading day (2026-08-20), full Journal page: Market Thesis → Watchlist (collapsed) → **Agent Analysis & Disagreements** (5 real candidates, specialist cards, ALIGNED/INSUFFICIENT DATA badges) → Runs this day → Trades → Daily Result. |

All six scenarios (plus the interaction/regression pass — session-chip
override, candidate drill-down from the Journal's collapsed list — see
Method) were captured with zero browser console errors or page errors.

## Method

Same accepted pattern as prior stages: `npm run dev` (Vite), proxying to
the real `qamc` production Mission Control API on `127.0.0.1:8800` for
everything except one new field. `risk_limits` doesn't exist on old
production yet (this branch adds it), so — following the exact precedent
`ops/preview/branch_preview.py` already establishes for "this branch adds
backend surface production hasn't deployed yet" — a small
`_risk_limits_config()` reconstruction was added to that file, reading
`config/settings.yaml`'s non-secret `risk` section directly (mirroring
`_cash_sweep_config()`'s existing pattern exactly, for the same reason:
sidesteps `get_config()`'s full `AppConfig` validation, which raises in
this credential-less `dev` sandbox). `branch_preview.py` was run locally
(`--host 127.0.0.1 --port 8813`, never a public/tailnet interface) for the
single verification pass that needed it (screenshot 04); `vite.config.ts`'s
dev proxy target was temporarily pointed at it and reverted immediately
after (confirmed via `git diff` showing zero net change to that file).
Every other screenshot in this stage proxies directly to real,
unmodified `qamc` production. Neither real production nor
`ops/preview/branch_preview.py`'s own already-established safety
boundaries (GET-only, non-loopback-refusing bind guard, no `qamc`
credential/DB access) were altered — only a second config-section reader
was added, following the file's own established pattern.

Screenshots captured with a local headless Playwright script
(`chromium.launch()`, no browser extension) run directly on this machine,
consistent with Stage 6b/6f/6g/6h/6i's established evidence convention.

## Checks performed

- [x] Per-candidate agent graph verified against two different real
      scenarios (AAPL: two aligned specialists; NVDA: single specialist,
      no alignment badge asserted) — confirms the alignment computation
      only ever reflects what the backend's own `consensus.agreement`
      supports, never a frontend guess.
- [x] Vertical-layout fix verified: the graph no longer clips/crushes in
      the narrow cockpit rail (screenshot 01/02 vs. the pre-fix capture
      taken during this pass, not committed, showing the horizontal
      layout's specialist card cut off at the panel edge).
- [x] "Run-wide" labeling verified present under the candidate-scoped
      graph so PM/Risk run-level cards can't misattribute to the titled
      symbol.
- [x] iPad Decision Room re-verified with the new per-candidate graph
      (screenshot 03), reusing the same `AgentFlowGraph` `fitView`/
      `ResizeObserver` fix Stage 6h already established for the
      hidden→visible tab-switch path.
- [x] `risk_limits` end-to-end verified: real config values (20/90/3/40)
      flow through `get_risk_limits()` → `_compute_risk_limits()` →
      `AccountResponse` → frontend gauge bands (screenshot 04), AND the
      graceful single-tone fallback verified against real unmodified
      production lacking the field (screenshot 05) — both paths real,
      neither fabricated.
- [x] `LevelBar` verified rendering the correct segment count for all
      three real conviction/confidence values observed (MEDIUM → 2/3,
      HIGH → 3/3, LOW → 1/3).
- [x] Journal Agent Analysis & Disagreements verified against a real
      multi-run trading day (2026-08-20): 5 notable candidates, real
      ALIGNED/INSUFFICIENT DATA badges, correctly zero false "mixed"
      claims where the backend's own data doesn't support one.
- [x] Regression: candidate drill-down from both the cockpit rail and the
      Journal's Agent Analysis cards still opens `CandidateDetailModal`
      correctly (manual interactive pass, consistent with Stage 6i's
      equivalent check — not re-screenshotted since the modal itself is
      unchanged this stage).
- [x] Zero browser console errors / zero page errors across every
      captured scenario.
- [x] `npm run build` (TypeScript strict + Vite) and `npm test` (Vitest —
      17/17, unchanged from Stage 6i; no new pure-function surface was
      added that warranted new unit tests this stage — the new backend
      accessor/schema fields are covered by the Python suite instead)
      both pass.
- [x] Python suite: targeted `tests/test_api_*.py` (129 passed) and the
      **full suite** (1997 passed, matching `docs/WORK.md`'s recorded
      baseline exactly) both re-run after the `src/api/` changes — zero
      regressions.
- [x] `git diff --stat -- src/ scripts/ config/ ops/` confirms the only
      backend files touched are `src/api/deps.py`, `src/api/schemas.py`,
      `src/api/routes_live.py` (the new read-only `risk_limits` field) and
      `ops/preview/branch_preview.py` (a `dev`-only, never-deployed
      verification tool) — zero `src/risk/`, `src/agents/`,
      `src/execution/`, `src/pipeline.py`, or config-file changes.
- [x] Real `qamc` production untouched (GET-only throughout); the one
      local `branch_preview.py` instance used for the `risk_limits`
      screenshot was bound to `127.0.0.1` only, stopped immediately after,
      and `vite.config.ts` shows zero net diff.
- [x] No secrets in screenshots, scripts, or this document.

## Known remaining product gaps (not addressed this stage)

- **Proposed → Executed Difference** as its own explicit Journal Day
  section (`docs/OUTCOME.md` item 7) is not yet a dedicated day-level
  block — the same delta detail (`CandidateDetailModal`'s
  `ProposedVsExecuted` table: proposed action/size/entry/stop vs. executed
  action/qty/price/stop) remains one click away from every notable
  candidate in both the Journal's Agent Analysis cards and the per-run
  cards, but isn't surfaced inline. Left out this pass rather than build
  a third rendering of that comparison at lower confidence; a reasonable
  next increment if the click-through proves insufficient in practice.
- **Learning Center** (`docs/WORK.md` priority 9 — scoreboards, Meta-
  Reflector prompt-diff/approve-reject presentation) remains unbuilt.
  `GET /reflections` explicitly does not parse Meta-Reflector free text
  yet (`docs/architecture/MISSION_CONTROL_API.md`), so a real Learning
  Center needs backend parsing work beyond "minimal read-only exposure of
  already-authoritative state" — flagged rather than forced through with
  a low-confidence text-scrape.
