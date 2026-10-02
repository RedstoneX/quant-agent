# Rebuild everything, properly — the standing plan

Owner mandate 2026-10-01: "EVERYTHING needs to be done NOW - properly."
This supersedes the earlier two-file split mandate. The desk stays dark until
it is done. This file is the durable plan; it survives session resets and
compactions, and is the first thing to read on resuming.

## The measurement

Ceiling enforced by `tests/file_size_baseline.json` is 2,561 lines.
16 files in `src/` exceeded it when this started. Re-measured against
origin/main 2026-10-02 03:0x: **14 remain, 21,663 lines above ceiling.**

| lines | file |
|------:|------|
| 7046 | src/execution/broker.py |
| 6270 | src/storage/db.py |
| 4438 | src/pipeline_protection.py |
| 4034 | src/agents/portfolio_manager.py |
| 3832 | src/pipeline.py |
| 3754 | src/pipeline_exits.py |
| 2610 | src/config.py |

Re-measured 2026-10-02: **7 files remain, 14057 lines above ceiling** (from 16 files / 26,951 lines).

DONE: `src/models.py` 5,265 -> package, largest 890. `src/notifier.py` 3,984
-> package, largest 592. `src/pipeline.py` 4,993 -> 3,832 by moving the
evening and position-review session bodies into `src/sessions/`.

`src/pipeline.py` breakdown (AST, origin/main): TradingPipeline is 4,549 of
the 4,993 lines across 61 methods. Five methods are half the file —
`_run_position_review_body` 666, `_run_evening_body` 613, `__init__` 520,
`_run_morning_body` 413, `_run_earnings_preprocess_body` 258. The 15 earlier
conversion steps never touched any of them, and no delegating stub was
deletable (measured: 4,993 before, 4,993 after).

## Doctrine for every piece

- Code MOVES VERBATIM. No behaviour change while splitting.
- A piece counts only if it can be constructed and exercised WITHOUT building
  a `TradingPipeline` (`tests/boundary_harness.py::check_boundary`).
- Prove moved code is unchanged; never assert it.
- Never weaken, skip, delete or xfail a test. Collected counts must match.
- Baselines may only shrink. A guard refusing a change is the guard telling
  the truth.
- Exactly one module-level `__getattr__` and one write-through `__setattr__`
  per file. Two mirror blocks silently cancel each other out.
- Money paths (broker, cost circuit, protection, exits, sizing) get an
  equivalence proof and an adversary pass before merge.

## Bugs found while splitting — parked, not fixed

See `docs/SPLIT_DEFERRED_FINDINGS.md`. They are fixed AFTER the structure is
sound, never woven into a split.
