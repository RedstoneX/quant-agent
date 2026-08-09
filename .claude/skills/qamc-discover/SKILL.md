---
name: qamc-discover
description: Explore and challenge the current QAMC architecture against the desired outcome before implementation. Use when the operator asks to design, rethink, challenge, or start a new substantial QAMC outcome.
---

# QAMC outcome discovery workflow

## Start with the outcome, not the old plan

Read:
1. `docs/OUTCOME.md`;
2. `docs/STATE.md`;
3. `docs/work/ACTIVE.md`;
4. `docs/decisions/ACTIVE.md`.

Then inspect only the source, tests, architecture, donor/UI material, and history needed to understand the current outcome.
Do not preload `docs/history/` or the entire repository.

## Role

Act as a senior engineering/architecture participant, not as an implementation worker waiting for instructions.

Independently explore the repository and challenge the existing plan against the desired outcome.
Do not preserve a prior architecture merely because it is documented.
Do preserve accepted hard safety/product boundaries unless your discovery explicitly identifies a reason they should be reconsidered.

No product implementation is allowed during this workflow.

## Investigate before asking

Use repository inspection and focused Explore/subagents aggressively for bounded factual discovery while keeping the lead context focused on synthesis.

Classify unknowns before asking anyone:

### A. Repository fact
Find the answer yourself from code, tests, Git history, docs, or available tooling.

### B. Routine engineering decision
Make the decision yourself using engineering judgment. Record a material choice only if the implementation session will need it.

### C. Operator product decision
Ask the operator only when the answer depends on their preference, intended experience, value judgment, or acceptable trade-off.

Ask **one question at a time and wait for the answer**. Do not batch an interview. After each answer, reassess whether another question is actually necessary.

### D. Material architecture / safety / governance issue
If the issue materially changes accepted architecture, safety boundaries, project scope, or a consequential technical trade-off that benefits from independent architecture review, do not make the operator solve it.
Record the issue, options, evidence, and your recommendation under `Architecture consultation` in `docs/work/ACTIVE.md` for ChatGPT review through GitHub.

## Challenge pass

Evaluate the current Mission Control plan and classify findings as:
- **KEEP** — strongest/simple current choice;
- **CHANGE** — right goal, better architecture/sequencing available;
- **REMOVE** — unnecessary, obsolete, duplicated, or too costly;
- **ADD** — missing capability needed for the outcome.

Challenge at least:
- whether current Stage 3–5 boundaries/sequencing still make sense;
- accepted Stage-2 API/data suitability for the desired UI/forensics;
- donor reuse versus native implementation;
- frontend/data-flow architecture;
- journal/search architecture;
- model/provider/decision observability presentation;
- testing and visual-verification strategy;
- whether existing Claude-native rules/skills/subagents help or constrain the work;
- unnecessary infrastructure or bespoke work;
- any assumption inherited from when Claude Code was treated mainly as a coder.

## Output

Update `docs/work/ACTIVE.md` with a concise evidence-based discovery result:
- repository findings;
- KEEP / CHANGE / REMOVE / ADD;
- operator decisions actually obtained;
- architecture consultation items, if any;
- a proposed implementation **outcome contract**: capabilities, constraints, and verifiable acceptance criteria.

Do not prescribe worker topology, file-by-file tasks, or detailed implementation steps. The fresh implementation session owns those decisions.

## Handoff

Commit/push the discovery branch and STOP before implementation.

The next step is external architecture reconciliation through GitHub by ChatGPT/operator. After that result is accepted and merged, implementation begins in a fresh Claude Code session using `/qamc-build`.
