# QAMC — Agent Entry Point

This file is for coding agents that use `AGENTS.md`. Claude Code uses native `CLAUDE.md` + `.claude/` configuration instead.

## Current source of truth

1. `docs/OUTCOME.md` — desired product/experiment result.
2. `docs/STATE.md` — current accepted state and authorization.
3. `docs/work/ACTIVE.md` — current discovery/implementation handoff contract.
4. `docs/decisions/ACTIVE.md` — operative decisions.
5. Relevant `docs/architecture/` files only when needed.

Do not treat `docs/history/` or legacy governance snapshots as current instructions.

## Current workflow

Substantial work is discovery-first:
- explore the actual repository and challenge the proposed plan against the outcome;
- resolve repository facts yourself;
- make routine engineering decisions yourself;
- ask the operator only genuine product/value questions, one at a time;
- route material architecture/safety conflicts through GitHub for ChatGPT reconciliation;
- do not implement until `docs/STATE.md` and `docs/work/ACTIVE.md` explicitly authorize implementation.

## Universal boundaries

- Alpaca Paper only; live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Deterministic Python risk/execution remains final authority and fails closed.
- Mission Control/API/journal/search/UI must remain non-critical and cannot directly execute broker writes during read-only phases.
- No secrets or fake production state.
- Derived search/journal state must be rebuildable.
- AgentLens is out of plan.
- No unnecessary infrastructure.
- Preserve upstream mergeability.

## Roles

- Operator: outcome/product owner and final acceptance authority.
- Claude Code: architecture/engineering participant during discovery; engineering lead during accepted implementation.
- ChatGPT: architecture challenger/reconciliation layer, independent checkpoint reviewer, GitHub governance/integration.
- GitHub: durable shared memory and handoff.
