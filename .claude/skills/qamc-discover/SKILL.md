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

Then inspect only the **accepted current contracts, source, tests, and specific reference evidence needed** to understand the outcome.
Do not preload `docs/reference/`, `docs/history/`, the Project Compass, or the whole repository.

Prior Mission Control UI/donor/journal work is under `docs/reference/mission-control/`. It is deliberately **challengeable evidence**, not a build specification. The path-scoped reference rule applies whenever it is opened.

## Role

Act as a senior engineering/architecture participant, not an implementation worker waiting for instructions.
Independently explore the repository and challenge the existing plan against the desired outcome.
Do not preserve a prior architecture merely because it is documented.
Preserve accepted hard safety/product boundaries unless discovery identifies a reason that genuinely requires external reconsideration.

**No product implementation is allowed during this workflow.**

## Investigate before asking

Use repository inspection and focused Explore/subagents for bounded factual discovery while keeping the lead context focused on synthesis.

Classify unknowns before asking anyone:

### A. Repository fact
Find it from code, tests, Git history, current contracts, or available tooling.

### B. Routine engineering decision
Decide it using engineering judgment. Record only material choices the implementation session will need.

### C. Operator product decision
Ask only when the answer depends on operator preference, intended experience, value judgment, or acceptable trade-off.

Ask **one question at a time and wait for the answer**. After each answer, reassess whether another question is actually necessary.

### D. Material architecture / safety / governance issue
Do not make the operator solve a technical problem. Record the issue, evidence, options, and recommendation under `Architecture consultation` in `docs/work/ACTIVE.md` for ChatGPT reconciliation through GitHub.

## Challenge pass

Classify findings as:
- **KEEP** — strongest/simple current choice;
- **CHANGE** — right goal, better architecture/sequencing available;
- **REMOVE** — unnecessary, obsolete, duplicated, or too costly;
- **ADD** — missing capability needed for the outcome.

Challenge at least:
- Stage 3–5 grouping/sequencing;
- Stage-2 API/data suitability for desired UI/forensics;
- donor reuse versus native work;
- frontend/data-flow architecture;
- journal/search architecture;
- model/provider/decision observability presentation;
- runtime/visual verification strategy;
- whether current Claude-native rules/skills/subagents help or constrain the work;
- unnecessary infrastructure or bespoke work;
- assumptions inherited from when Claude Code was treated mainly as a coder.

## Output

Update `docs/work/ACTIVE.md` with a concise evidence-based discovery result:
- repository findings;
- KEEP / CHANGE / REMOVE / ADD;
- operator decisions actually obtained;
- architecture consultation items, if any;
- proposed implementation **outcome contract**: capabilities, constraints, and verifiable acceptance criteria.

Do not prescribe worker topology, file-by-file tasks, or a detailed implementation recipe. The fresh implementation session owns those decisions.

## Operator Compass refresh

Before handoff, refresh `docs/knowledge/PROJECT_COMPASS.md` from authoritative live files.
Follow `.claude/rules/documentation.md`; keep the Compass plain-English, visual, emoji-landmarked, concise, and focused on what finished / now / next / later / decisions.
Do not copy the technical discovery report into it.

## Handoff

Commit/push the discovery branch and STOP before implementation.
The next step is external architecture reconciliation through GitHub by ChatGPT/operator. After that result is accepted and merged, implementation begins in a fresh Claude Code session using `/qamc-build`.
