# QAMC Current State

Updated: 2026-08-12

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC can reach Alpaca Paper through the approved OneCLI path; `/health` reports `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- OpenRouter transport is commissioned. The current all-agent `openai/gpt-5.5` mapping is the commissioning **baseline**, not the final cost-optimized model policy.
- Alpaca Paper-only is enforced in code.
- PR #27 is externally reviewed and merged into `main` as `63cca1a1445757b63376d9816cccf48d4d1b0c58`.
- Trading timers remain disabled.

## Authorized now

Claude is authorized to complete the sequence in `docs/WORK.md` autonomously:

1. close the final runtime-account commissioning checks after the merged tooling reaches `/home/qamc/quant-agent`;
2. research and implement a **cost-optimized, multi-model OpenRouter policy** for QAMC;
3. validate quality, cost, safety and full regression evidence before returning for external review.

The intended direction is explicit and auditable model selection: inexpensive capable models (including current Qwen/DeepSeek candidates where evidence supports them) for routine specialist work, stronger models only where their additional reasoning quality justifies the cost. Per-agent mapping and bounded complexity/escalation rules are authorized when measurable and reviewable. Silent or opaque model switching is not.

Claude may research current OpenRouter model availability/pricing, benchmark candidates, implement within the existing provider/model seam, test, debug and make routine engineering choices without operator involvement.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Trading timers stay disabled until this tranche passes external review.
- No deterministic trading/risk semantic redesign.
- No broker-write Mission Control controls.
- No public exposure of QAMC or OneCLI.
- Do not collapse `dev` / `qamc` / `ubuntu` boundaries.
- No replacement credential gateway/vault/proxy.
- No new durable routing platform or unnecessary infrastructure; use the existing QAMC/OpenRouter seams.
- No secrets in Git, chat, logs, screenshots or client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.

## Handoff

Claude Code works from `/home/dev/projects/quant-agent`. Runtime changes under `/home/qamc` must preserve account isolation.

Proceed through `docs/WORK.md` until the verified finish line or a genuine operator-only boundary: required privilege, required secret entry, or a material architecture/product conflict.
