---
name: qamc-discover
description: Challenge a substantial QAMC outcome against the actual repository before implementation.
---

# QAMC discovery workflow

## Load the minimum

Read only:
1. `docs/OUTCOME.md`;
2. `docs/STATE.md`;
3. `docs/WORK.md`.

Then inspect only source, tests and accepted architecture needed to answer the current problem. Use targeted Git history only when current evidence is insufficient.

## Role

Act as an architecture/engineering participant. Inspect the real repository and challenge the prior direction rather than defending it.

**No product implementation during discovery.**

Resolve repository facts yourself. Make routine engineering decisions yourself. Ask the operator only genuine product/value questions, one at a time. Put material architecture/safety/scope conflicts into `docs/WORK.md` for ChatGPT reconciliation instead of asking the operator to solve technical choices.

## Challenge

Classify material findings:
- **KEEP** — already the simplest/best fit;
- **CHANGE** — right goal, better route exists;
- **REMOVE** — unnecessary, duplicated, stale or too costly;
- **ADD** — missing capability required by the outcome.

Challenge architecture/sequencing, accepted Stage-2 data/API suitability, reuse versus custom work, UI/data-flow and forensic-history needs, verification strategy, unnecessary infrastructure, and assumptions inherited from earlier plans. Do not revive old donors/components/stages merely because Git history contains them.

Use focused inexpensive subagents for bounded repository search/high-volume reading when useful. Escalate model/worker strength only when the task genuinely needs it.

## Output

Update `docs/WORK.md` with concise evidence for:
- repository findings;
- KEEP / CHANGE / REMOVE / ADD;
- operator decisions actually obtained;
- architecture consultation items, if any;
- a proposed implementation outcome contract with capabilities, constraints and verifiable acceptance conditions.

Do not prescribe worker topology or a file-by-file implementation recipe.

Before handoff, refresh `docs/PROJECT_COMPASS.md` from the authoritative files.

Commit/push the discovery branch and **STOP before implementation**. ChatGPT/operator reconcile the GitHub result. After accepted merge, implementation starts in a fresh Claude session using `/qamc-build`.
