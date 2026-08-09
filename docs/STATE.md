# QAMC Current State

Updated: 2026-08-09

This file says what is accepted and authorized **now**. Git history preserves prior state.

## Accepted

- Stages 0, 0.5, 1 and 2 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Accepted Stage-2 full-suite result: **1530 passed, 0 failed**.
- AgentLens remains removed.
- Default runtime posture remains small Linux VPS/server + private access; avoid distributed infrastructure without demonstrated need.

## Authorized now

**Discovery R1 only:** independently challenge the post-Stage-2 Mission Control direction against `docs/OUTCOME.md` and the actual repository.

Current assignment and durable handoff: `docs/WORK.md`.

Claude may inspect, investigate, challenge, update the discovery result/Compass, and push a dedicated discovery branch.

## Not authorized now

- Mission Control product implementation;
- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading.

## Handoff

Discovery ends with a pushed branch and **STOP**. ChatGPT/operator reconcile and accept/reject the result. After an accepted merge, implementation begins in a fresh Claude Code session from GitHub; the discovery transcript is not required.
