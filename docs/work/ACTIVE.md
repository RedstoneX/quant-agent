# QAMC Active Work Contract

Status: **DISCOVERY / ARCHITECTURE CHALLENGE**

This is the single durable handoff for the current piece of work. Git history preserves earlier versions; do not create a chain of competing active briefs.

## Current outcome

Re-evaluate the proposed Mission Control Stages 3–5 against `docs/OUTCOME.md` and the actual repository **before implementation**.

The purpose is to take advantage of Claude Code as an engineering/architecture participant, not merely as a coder executing a plan created without its consultation.

## Discovery assignment

Claude Code should independently inspect the repository, accepted Stage-0–2 implementation, API/data surfaces, architecture documents, UI donor material, tests, and current Claude-native tooling.

Challenge the current plan rather than defending it. Determine what should be:
- **KEEP** — already the simplest/best fit;
- **CHANGE** — direction is right but architecture/sequencing should improve;
- **REMOVE** — unnecessary complexity or obsolete assumption;
- **ADD** — missing capability necessary to achieve the outcome.

No Mission Control implementation is authorized during this discovery pass.

## Question routing

Do not turn the operator into the technical architect.

1. **Repository facts / existing capabilities** → investigate them yourself.
2. **Routine engineering choices** → decide them yourself.
3. **Product preference or value trade-off only the operator can answer** → ask the operator **one question at a time and wait for the response**.
4. **Material architecture/safety/governance question that requires external reconciliation** → record it under `Architecture consultation` below for ChatGPT review through GitHub; do not ask the operator to solve a technical problem.

Do not ask a question merely because asking is easier than investigating.

## Discovery output

At the end of discovery, replace the placeholder sections below with a concise evidence-based proposal. Do not write implementation recipes or assign files/workers in advance.

### Repository findings

_Pending Claude discovery._

### KEEP / CHANGE / REMOVE / ADD

_Pending Claude discovery._

### Operator product decisions

_Pending. Record only decisions actually obtained from the operator._

### Architecture consultation

_Pending. Record only material issues that Claude cannot responsibly settle inside accepted boundaries._

### Proposed implementation outcome contract

_Pending. State capabilities, constraints, and verifiable acceptance conditions—not a step-by-step coding plan._

## Handoff rule

When discovery is complete, Claude must commit/push this branch and **STOP before implementation**.

ChatGPT independently reviews the actual repository findings and proposed contract, reconciles material architecture questions, and presents any genuine product decisions to the operator. Once reconciled and accepted, GitHub is updated and merged.

Implementation then begins in a **fresh Claude Code session** from the accepted GitHub state. The implementation session should not need the discovery conversation transcript.
