# QAMC Current State

Updated: 2026-08-09

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1 and 2 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Accepted Stage-2 full-suite result: **1530 passed, 0 failed**.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Product direction for Stage 4 is **per-candidate fidelity from the start**, while preserving each specialist's real data scope rather than inventing a uniform per-symbol schema.
- Research evidence remains correlated by `run_id`/natural scope; `decision_id` remains PM → AI Risk → trade correlation.
- AgentLens remains removed.
- Default runtime posture remains small Linux VPS/server + private access; avoid distributed infrastructure without demonstrated need.

## Authorized now

**Stage 3 only: Trading cockpit.**

Current assignment and durable handoff: `docs/WORK.md`.

Claude may implement the Stage-3 cockpit on a dedicated branch using `/qamc-build`, verify it, update the Compass, push, and stop for external review.

## Not authorized now

- Stage 4 specialist-evidence persistence/decision-interface work;
- Stage 5 journal/search implementation;
- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading.

## Handoff

Stage 3 ends with a pushed branch and **STOP**. ChatGPT/operator review the actual implementation before Stage 4 can be authorized.
