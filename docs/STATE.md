# QAMC Current State

Updated: 2026-08-09

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1, 2 and 3 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Stage 3 delivered the read-only browser/iPad Trading Cockpit at `/ui`; accepted verification: **1531 passed, 0 failed** plus desktop/iPad runtime review.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Stage-4 product direction is **per-candidate fidelity**, while preserving each specialist's real data scope rather than inventing a uniform per-symbol schema.
- Research evidence remains correlated by `run_id`/natural scope; `decision_id` remains PM → AI Risk → trade correlation.
- AgentLens remains removed.
- Default runtime posture remains small Linux VPS/server + private access; avoid distributed infrastructure without demonstrated need.

## Authorized now

**Remaining read-only Mission Control tranche through Stage 5.**

Current assignment and durable handoff: `docs/WORK.md`.

Claude may implement Stage 4, close it as an **internal** checkpoint, and continue directly into Stage 5 in the same engineering session/branch when practical. Numbered stages are implementation slices, not automatic permission boundaries.

Use `/qamc-build`. Stop early only for a genuine operator product/value decision that cannot be resolved from the accepted outcome, a material architecture/safety/scope conflict, or evidence that invalidates the accepted direction.

## Not authorized now

- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading;
- later learning/write-control stages beyond the current read-only Mission Control tranche.

## Handoff

The next mandatory external gate is after the **complete remaining Mission Control tranche (Stage 5)**. At that point Claude verifies the integrated result, pushes, and **STOPS** for ChatGPT/operator review. Stage 4 alone does not require an external stop.
