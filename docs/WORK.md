# QAMC Current Work

Status: **FINAL COMMISSIONING CHECK + COST-OPTIMIZED MODEL ROUTING**

## Goal

Finish the deployed QAMC MVP with an explicit, auditable and materially cheaper OpenRouter model policy, while preserving the accepted trading engine, safety chain, OneCLI credential architecture and account isolation.

The current `openai/gpt-5.5` mapping for all agents is the commissioning baseline, not the target production-paper policy.

## Required sequence

### 1. Close commissioning evidence

From current `main`, ensure the merged commissioning tooling reaches the runtime checkout and run the acceptance command from both required account contexts as documented in `ops/onecli/README.md` step 4e.

Proceed only if the combined evidence is complete and clean. Trading timers remain disabled.

### 2. Determine the best current model policy

Use current OpenRouter availability, pricing and capability evidence rather than stale model assumptions.

Evaluate cost-efficient candidates including current Qwen and DeepSeek offerings, plus any clearly superior current alternative. Compare them against the `openai/gpt-5.5` baseline on representative QAMC agent tasks.

Optimize for **decision quality per dollar**, not cheapest-token price alone.

### 3. Implement through the existing routing seam

Use QAMC's existing explicit per-agent provider/model configuration and provider abstraction. Do not add a new routing service or infrastructure layer.

Authorized policy forms:

- explicit per-agent model mapping;
- stronger models reserved for roles/tasks that demonstrably benefit from them;
- bounded complexity/escalation or fallback rules when deterministic, auditable and evidence-backed.

Every invocation must remain attributable to the actual model used. No silent fallback, opaque auto-routing or unrecorded model choice.

### 4. Verify quality and economics

Build a reproducible comparison sufficient to answer:

- which model/policy each agent uses and why;
- whether specialist, Portfolio Manager and AI Risk Manager outputs remain fit for their roles;
- expected cost reduction versus the all-`gpt-5.5` baseline;
- how escalation/fallback behaves, if used;
- whether failures still fail closed and remain observable.

Do not optimize cost by weakening deterministic safety controls or changing trading/risk semantics.

### 5. Regression and external gate

Before stopping:

- full applicable test suite passes;
- commissioning verification remains clean;
- Alpaca resolves to Paper only;
- OneCLI/private/account-isolation boundaries remain intact;
- Mission Control remains read-only;
- no secrets leak;
- trading timers remain disabled;
- model-routing evidence is reproducible and concise.

Commit and push a dedicated Claude branch, then stop for ChatGPT external review. Claude must not merge its own work.

## Autonomy

Claude owns the engineering loop: research, benchmark, implement, debug, test and make routine technical choices without asking the operator.

Stop only for:

1. a genuinely required privileged action;
2. required entry of a real secret;
3. a material architecture/product decision outside this authorized routing direction.

Bundle any operator-only intervention into one concise request.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Trading timers remain disabled until external review approves activation.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No broker-write Mission Control controls.
- No public services.
- No collapse of `dev` / `qamc` / `ubuntu` isolation.
- No replacement for upstream OneCLI.
- No new durable routing infrastructure when the existing QAMC/OpenRouter seam is sufficient.
- No secrets in Git/chat/logs/screenshots/client evidence.
