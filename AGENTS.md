# QAMC — Agent Entry Point

This file is for coding agents that use `AGENTS.md`. Claude Code uses the native `CLAUDE.md` + `.claude/` configuration instead.

## Current source of truth

1. `docs/STATE.md` — current accepted state and authorization.
2. `docs/decisions/ACTIVE.md` — active architecture/product decisions.
3. Relevant file(s) under `docs/architecture/`.
4. `docs/ROADMAP.md` when planning or closing a gate.

Do not treat `docs/history/` or legacy governance snapshots as current instructions.

## Universal boundaries

- Alpaca Paper only; live trading is not authorized.
- `yebof/quant-agent` remains the authoritative trading engine.
- Deterministic Python risk/execution remains final authority and fails closed.
- Mission Control/API/journal/search/UI must remain non-critical and cannot directly execute broker writes.
- No secrets or fake production state.
- Derived search/journal state must be rebuildable.
- AgentLens is out of plan.
- No unnecessary infrastructure.
- Preserve upstream mergeability.

## Roles

- Operator: product/experiment owner and final acceptance authority.
- Claude Code: engineering lead/orchestrator for authorized implementation.
- ChatGPT: architecture challenger, independent checkpoint reviewer, governance/GitHub integrator.

Implementation detail belongs to the engineering lead. External acceptance and merge remain separate.
