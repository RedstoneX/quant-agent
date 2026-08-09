# QAMC Current Work

Status: **STAGE 3 IMPLEMENTATION AUTHORIZED**

## Goal

Build the smallest polished browser/iPad **Trading Cockpit** over the accepted Stage-2 read-only API.

Use `/qamc-build`.

## Required outcome

- Real API-backed account/equity/P&L, positions, orders, trades, health, and existing watchlist-candidate feed.
- Clear Alpaca **Paper** identity.
- Honest empty/loading/error/degraded states.
- No production mock fallback.
- Responsive desktop + representative iPad experience.
- `/candidates` must be labeled as the watchlist/expansion feed, not as every symbol considered during a trading run.

## Boundaries

- No trading/risk semantic changes.
- No broker-write Mission Control operations.
- No Stage-4 per-candidate specialist persistence or decision-interface work yet.
- No Stage-5 journal/search work.
- Frontend stack, charting library, and old donor projects are implementation choices, not requirements. Choose the smallest maintainable approach that satisfies the outcome.
- Mission Control remains read-only, non-critical to trading, and safe to fail independently.
- No secrets or fake production trading state in client/UI surfaces.

## Acceptance

- Relevant cockpit data is backed by the accepted Stage-2 API and correctly labeled.
- Running visual verification at desktop and representative iPad viewport.
- Verify populated plus empty/loading/error/degraded states.
- Existing backend suite remains green.
- Run frontend build/lint/type/test checks that apply to the implementation chosen.
- Refresh `docs/PROJECT_COMPASS.md` before handoff.

## Future accepted direction — not authorized in this build

- Stage 4 will provide per-candidate specialist evidence, preserving native scopes: tech/earnings are naturally symbol-oriented; news mixes symbol and broader context; macro is run/sector-level. Do not fabricate a universal per-symbol agent schema or change existing `decision_id` semantics.
- Stage 5 remains journal/forensic-search capability to be re-evaluated when Stage 4 is accepted.

## Handoff

Commit and push the Stage-3 implementation branch, record verification evidence concisely, and **STOP**. ChatGPT/operator review the actual GitHub implementation before Stage 4 is authorized.
