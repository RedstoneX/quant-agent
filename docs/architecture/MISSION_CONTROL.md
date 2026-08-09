# Mission Control Architecture Boundary

Status: **post-Stage-2 design is provisional during Discovery R1**.

This file defines the accepted boundaries around Mission Control. It does **not** freeze the prior Stage 3–5 frontend stack, donor choices, screen layout, or sequencing.

## Accepted foundation

- `yebof/quant-agent` remains the authoritative trading engine.
- The accepted Stage-2 API is the current read-only seam into trading/account/history state; see `MISSION_CONTROL_API.md`.
- Mission Control is presentation/operator UX and must remain non-critical to trading.
- Current Mission Control work is read-only; it cannot place/cancel/modify/bypass broker orders.
- Derived UI/journal/search state must be rebuildable and non-authoritative.
- The operator must be able to distinguish proposed AI intent, AI Risk intervention, deterministic risk outcome, and what actually executed.
- Real provider/model/cost/correlation evidence comes from canonical QAMC records rather than a second telemetry store.

## Desired product capabilities

The current outcome calls for a polished browser/iPad experience that can make the following understandable without raw logs:
- account/equity/P&L, positions, orders, trades, candidates, and health;
- specialist agent conclusions and disagreement;
- Portfolio Manager proposal;
- AI Risk changes/rejections;
- deterministic gate result;
- proposed-versus-executed delta;
- model/provider/cost/latency/fallback attribution;
- prior-day journal/forensic history.

These are **capabilities**, not a prescribed component hierarchy.

## Discovery R1 questions

Claude should independently determine the simplest architecture that reaches the outcome, including:
- whether the previously proposed frontend stack is still the best fit;
- whether prior donor research saves work or creates adaptation cost;
- whether current Stage-2 reads are sufficient or need minimal additional read-side support;
- how much journal/search functionality belongs in the same implementation tranche;
- the cleanest responsive information architecture for desktop/iPad;
- how to verify the interface with real API-backed data and degraded/error states.

Prior UI/donor/journal proposals are preserved under `docs/reference/mission-control/` as **challengeable research input**, not implementation instructions.

## Non-goals during current discovery

No product implementation, writable Mission Control operations, deterministic risk redesign, second trading engine, second authoritative memory system, or live trading.
