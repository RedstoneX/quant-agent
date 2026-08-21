# Stage 6k — professional visual convergence (primary cockpit)

A dedicated visual-composition pass on the primary desktop cockpit, following
the operator's explicit direction that Stage 6i/6j landed real functional and
truth-debt work but the rendered product still failed basic professional
visual-design standards: small text in large containers, inconsistent
typography, excessive dead space, the Decision Room agent-flow graph and the
risk-exposure gauge appearing comically undersized relative to their
containers, weak proportional/grid relationships, and sparse states that
preserved giant empty regions instead of recomposing.

Per the operator's mid-pass course-correction, this stage treats desktop
(1440×900 and 1600×1000) as the design source of truth; iPad received a
focused regression check at the end rather than parallel design work, and
phone was out of scope.

**Captured against:** commit `027707a` (this stage's work committed
together with this evidence, per this project's established convention).

**Verification date:** 2026-08-21, ~09:00–09:47 UTC.

## Governance model used for this pass

Per the operator's explicit direction mid-pass, implementation work was
governed by a multi-agent review structure rather than direct, single-pass
hand-editing: an independent Design Orchestrator agent inspected the rendered
cockpit, the vision board, and product docs, and wrote a page-level plan with
a concrete, mechanically-checkable acceptance checklist; a separate
implementer agent executed that plan; a separate, adversarial Visual QA
agent independently re-measured the rendered result and reported findings
back to the Orchestrator; the Orchestrator ruled reject-with-scoped-directive
once, a follow-up implementation pass landed, and a final round of direct,
coordinator-level measurement (see "A measurement correction," below) caught
and corrected a factual error two agents had independently made before a
final ruling accepted the result as a documented, bounded limitation rather
than a defect. This process (plan → implement → independent QA → ruling,
twice) is itself part of this stage's evidence, not just its outcome.

## What changed

1. **Risk-exposure gauge** (`HeroBand.tsx`/`ArcGauge.tsx`) — enlarged
   (`height={132}→205`) and re-centered (asymmetric padding removed) so the
   gauge fills its hero-band cell instead of floating in ~90px of empty
   padding on each side; fixed a pre-existing label/ring overlap bug.
2. **Decision Room agent-flow graph** (`agentflow/{nodes,buildGraph,
   AgentFlowGraph}.tsx`) — three compounding fixes: (a) specialist/stage/gate
   node handles were hardcoded to Left/Right regardless of layout direction,
   which made React Flow's edges swoop/cross when nodes were actually stacked
   top-to-bottom in the cockpit's narrow rail — handles are now
   direction-aware; (b) node cards widened (208/184/192px →
   260/240/248px) and row spacing re-tuned (empirically, after the first
   attempt produced real card overlap) so cards fill their ~340px lane
   instead of leaving 55-90px of dead space on each side; (c) React Flow's
   `fitView` padding (0.2, untouched by the sizing fix above) was silently
   discounting all of that re-tuning by ~4-5% — reduced to 0.08, the low end
   of an independently-verified range, after confirming smaller values
   monotonically help with no downside.
3. **Sparse/no-session state recomposition** — the single worst offender
   found: two of three primary columns previously rendered as a full
   viewport-height near-black rectangle around three lines of small dim
   text. `CandidateRail` and `DecisionRoomPanel` now collapse to their real
   content height (~285px, was ~750-850px) when there is truthfully no
   session yet, the chart column claims the freed width (+19%), and each
   collapsed panel renders a 44px geometric glyph (reusing
   `DecisionStateBanner`'s existing ○/●/◐/■ vocabulary) alongside its
   message instead of text alone.
4. **Viewport height budget** — `App.tsx`'s primary 3-column grid used a
   hardcoded `calc(100vh-150px)` that was stale against the real header-stack
   height (measured 423-526px across this pass as other fixes changed its
   height); replaced with a live `ResizeObserver`-driven `--chrome-h` CSS
   custom property so the row's height budget is always correct rather than
   silently drifting the next time a conditional header row changes.
5. **Market Regime deduplication** — `DecisionRoomPanel` previously repeated
   `HeroBand`'s entire regime pill/outlook/confidence/summary block
   word-for-word; collapsed to a one-line cross-reference, freeing
   ~110-140px of the Decision Room column for chain content.
6. **Typography consolidation** — ~17 uncoordinated arbitrary `text-[…rem]`
   values across the primary cockpit's components replaced with a 7-tier
   scale (`--fs-micro` 11.2px through `--fs-hero`, the equity figure), with
   an 11.2px hard floor (the pre-existing minimum, measured, was 9.6px).
7. **Candidate funnel label clipping** — the ECharts funnel's value-
   proportional sizing clamped narrow late-stage bands (e.g. "Reached PM 5")
   below their own label's width, clipping the count; raised the minimum
   band-size floor so every stage's label renders in full.
8. **Component-audit outcome** — per the operator's explicit direction to
   prefer proven component patterns over bespoke ones for ordinary dashboard
   UI, every major element on the primary cockpit was independently
   classified (retain / restyle / replace / genuinely-custom) against
   Tremor (already an installed dependency) and general professional
   dashboard patterns. Outcome: **zero new dependencies** — every element
   was found to already be either a correct, working, appropriately-scoped
   use of an existing proven library (ECharts funnel/gauge, lightweight-charts
   candlesticks, TanStack Table, React Flow) or a case where the "bespoke"
   implementation is bespoke *because* it encodes a genuine QAMC-specific
   truth constraint (`LevelBar`'s discrete non-fabricated confidence
   segments; the Deterministic Gate's categorically-different hexagon shape)
   that no generic library component could express without either
   fabricating precision or erasing the distinction the product needs.

## Known, documented limitation (not fixed this pass)

At 1440×900 specifically, for a typical 2-specialist candidate (real AAPL
data, 2026-08-20), the Portfolio Manager node in the Decision Room chain
requires ~62px of scroll within its own column before it's visible — it is
**not** unreachable (the column's internal scroll is genuine and functional;
Risk Manager/Deterministic Gate/Execution below it were always expected to
require this same scroll) but does not clear the fold entirely unscrolled,
which the original plan's acceptance bar asked for. At 1600×1000 this
clears cleanly. Root cause: the graph's own content height for a 2-specialist
candidate, combined with the corrected (accurate, no longer stale) chrome
budget at the shorter viewport height, leaves less internal room than a
strict zero-scroll bar needs. Closing this fully would require reopening
either the graph's row-spacing constants or the chrome-height mechanism —
both independently verified clean/correct elsewhere in this pass — for a
~62px margin on one node at one specific size. Judged not worth reopening
verified-correct work for; recorded here rather than silently accepted.

### A measurement correction (recorded for process transparency)

Two review agents in this pass's governance chain independently measured
this same gap and got different, and it turns out both wrong, numbers
(48.9px and 146px) — both had measured the "overshoot" against the wrong
DOM element (the raw viewport, and an unrelated container, respectively,
neither of which is what actually clips this content). Direct verification
by the coordinating session found the real clipping container
(`.panel-body`, confirmed via `getBoundingClientRect()`/`scrollHeight`/
`clientHeight` to be a genuine, working scroll region — not a false one) and
the correct number (~62.5px at 1440×900, a clean pass with ~37.5px of margin
at 1600×1000). A final ruling agent independently reproduced this corrected
measurement from scratch before accepting it. Noted here because it's a
real example of why this pass's evidence is graded on independent
re-measurement of the rendered result, not on trusting any single agent's
self-report — including this document's own claims, which is why every
number above is reproducible via the commands below.

## Screenshots in this directory

| File | Scenario |
|---|---|
| `00-before-desktop-1440x900-sparse.png` | Before this stage — real live pre-market state, desktop |
| `00-before-ipad-820x1100-sparse.png` | Before this stage — real live pre-market state, iPad |
| `01-desktop-1440x900-populated.png` | After — populated session (real 2026-08-20 data replay), desktop |
| `02-desktop-1440x900-sparse.png` | After — real live pre-market sparse state, desktop |
| `03-desktop-1600x1000-populated.png` | After — populated session, desktop, secondary target size |
| `04-desktop-1600x1000-sparse.png` | After — real live sparse state, desktop, secondary target size |
| `05-decision-room-full-chain.png` | After — full Specialists→PM→Risk→Gate→Execution chain, uncropped (tall viewport), showing the corrected connector-line routing |
| `06-ipad-820x1100-populated.png` | After — iPad regression check, populated |
| `07-ipad-820x1100-sparse.png` | After — iPad regression check, sparse |
| `08-journal-1440x900.png` | After — Journal Day view, spot-check (typography-token inheritance only; Journal's own composition was out of this pass's primary scope) |

All "after" screenshots: dark mode (this product's primary/intended theme —
`styles/index.css`'s unqualified `:root` is the dark palette), real QAMC
data throughout (live pre-market data for the sparse states; a real past
trading day, 2026-08-20, replayed via the branch-preview server's own
API for the populated states — never fabricated/mocked).

## Functional verification

- [x] `cd frontend && npm run build` — clean, no TypeScript errors.
- [x] `cd frontend && npm test` — 17/17 passed (2 test files), unchanged
      from before this stage.
- [x] `python -m pytest tests/test_api_safety.py` — 36/36 passed (GET-only
      enforcement etc.) — confirms zero backend behavior change; this stage
      touched no Python source, only `frontend/src/` and the resulting
      `src/api/static_cockpit/` build output.
- [x] Zero browser console/page errors across every captured scenario
      (one pre-existing, unrelated favicon/font 404 present before this
      stage as well — not a regression).
- [x] Real read-only QAMC data used throughout (branch-preview server,
      `127.0.0.1:8811`, GET-only, proxied to the real production API) — no
      fabricated data at any point in this pass, including verification.
- [x] No fabricated confidence percentages, invented risk scores, or false
      candidate attribution introduced — `LevelBar`'s discrete 3-segment
      indicator is unchanged in kind (only its label text sizes moved onto
      the new typography scale); the risk gauge's bands remain tied to
      `account.risk_limits.max_total_position_pct`, the real deterministic
      ceiling.
- [x] Zero new frontend dependencies. Zero changes under `src/` (Python
      backend) beyond the expected `static_cockpit` build-output
      regeneration.

## Reproducing these numbers

The branch-preview server (`ops/preview/branch_preview.py`, read-only,
GET-only, proxied to the real production API) serves this branch's build.
A populated state without waiting for a live session: intercept the
`/journal/{today}` request and replay `/journal/2026-08-20`'s real response
(the technique used throughout this pass — see this stage's commit history
for the exact Playwright pattern). `getBoundingClientRect()` /
`getComputedStyle()` against the live rendered page is how every specific
number in this document and this stage's development was produced —
never estimated from a screenshot alone.
