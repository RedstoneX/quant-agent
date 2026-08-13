# QAMC Current State

Updated: 2026-08-12

This file says what is accepted and authorized **now**. Git history preserves prior detail.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Mission Control/API and browser cockpit are deployed under `qamc`, private, read-only and non-critical to trading.
- The OVH account boundary remains: `ubuntu` = administration/recovery, `qamc` = runtime, `dev` = development/Claude Code.
- VPS baseline hardening and upstream OneCLI credential-gateway deployment are complete.
- QAMC can reach Alpaca Paper through the approved OneCLI path; `/health` reports `broker_reachable: true`, `db_reachable: true`, `paper: true`.
- OpenRouter transport is commissioned. The all-agent `openai/gpt-5.5` mapping was the commissioning **baseline**; the cost-optimized replacement is proposed in `docs/architecture/MODEL_ROUTING_POLICY.md` and awaits external review.
- Alpaca Paper-only is enforced in code.
- PR #27 is externally reviewed and merged into `main` as `63cca1a1445757b63376d9816cccf48d4d1b0c58`.
- Trading timers remain disabled during engineering/verification so scheduled paper-trading runs cannot start prematurely.

## ChatGPT GitHub integration role — reconstitution rule

When QAMC is being managed from ChatGPT, **ChatGPT owns GitHub review/integration and should use the connected GitHub plugin directly** for repository reads/writes, PR creation/review, merges, and routine GitHub administration whenever that connector supports the action.

Do **not** send routine GitHub work to the operator, Claude, Codex/Work mode, or another environment merely because a generic file/code handoff is offered. Use another path only when the GitHub connector genuinely lacks the required capability or the operator explicitly requests it.

Claude does not merge its own work. The operator should not be asked to perform routine GitHub housekeeping that ChatGPT can perform through the connector.

## At the external gate

The routing tranche is implemented and pushed on `claude/cost-optimized-model-routing-h4k2vn`. It awaits ChatGPT external review; Claude does not merge it.

What it delivered:

1. Per-seat model policy through the existing `config/settings.yaml` seam — no routing infrastructure added. Evidence and limitations: `docs/architecture/MODEL_ROUTING_POLICY.md`.
2. Working cost telemetry. Under the baseline, `estimate_cost` could not price an OpenRouter `vendor/model` id at all, so every call persisted `cost_usd = NULL` and every session rendered `$?.??`.
3. Projected LLM spend of **$72.10 → $1.13 per month** (98.4%) at measured-equal quality on 148 graded trials.

Two items are **not** closed and are the operator's:

- **OpenRouter credit is nearly exhausted** ($10 granted, $7.96 used, $2.04 left). At `max_tokens: 128000` the baseline model reserves $3.84 per call, so as commissioned no agent call could start — every one returns a non-retryable 402. The new policy reserves $0.05 per call, but credit must still be topped up before any live session.
- The `qamc`-account half of commissioning acceptance still needs running; `dev` cannot `sudo`.

## Not authorized without a new contract

- Enabling trading timers.
- Merging the routing branch.
- Any second provider or fallback model (deliberately not added — see the policy doc).

## Timer activation rule

Timer activation is **not a separate architecture or product-design problem**. The timers simply start QAMC's scheduled autonomous Alpaca Paper runs.

Keep them off during engineering and external review. After the final tranche is accepted and the operator authorizes the paper-soak start, enabling the timers is a routine deployment action. Claude should verify they remain off while work is incomplete, but should not repeatedly analyze or escalate timer activation as a separate decision.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
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
