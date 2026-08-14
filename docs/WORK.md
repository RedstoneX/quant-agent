# QAMC Current Work

Status: **FINAL RUNTIME ACCEPTANCE → PAPER SOAK**

## Goal

Start scheduled **Alpaca Paper trading** as soon as the final runtime commissioning rerun is green. Do not add another agent-intelligence, prompt, model-routing or dashboard-polish tranche before soak start unless runtime evidence exposes a genuine blocker.

The operator has explicitly authorized the paper soak once commissioning passes.

## Already closed

- Stages 0–5 are accepted and deployed.
- Mission Control is private, read-only and sufficient for initial soak observability.
- Cost-optimized OpenRouter routing is accepted: eight seats on `google/gemini-2.5-flash-lite`, `risk_manager` on `qwen/qwen3-235b-a22b-2507`.
- The decision-chain audit is accepted.
- Alpaca Paper connectivity, market data, FRED and both policy-model completions pass through OneCLI.
- Runtime health is green for DB, paper mode and broker reachability.
- The `dev` commissioning half is already green and proves the off-account credential-isolation boundary.
- Direct runtime inspection confirmed all seven trading timers are disabled.
- PR #33 fixed the verifier bug that misread systemd `disabled enabled` output and merged to `main` as `aa52f5f9fd5912914a1640f74bdab84d1e30cd51`.

## Immediate remaining work

One final `qamc` runtime acceptance rerun remains against current `main`.

Acceptance condition:

- zero FAIL results and process exit `0`;
- the dev-only isolation check may remain `SKIP` / single-account coverage may remain `partial` because that obligation is already proved by the green `dev` run;
- full commissioning is the union of the green `dev` and `qamc` evidence.

The previous runtime run was **36 PASS / 1 FAIL / 1 SKIP**. Every functional, credential, broker, model and Mission Control check passed. The sole FAIL was the now-fixed timer-state parser.

If the corrected runtime run is green, commissioning is complete. The already-recorded operator authorization then makes paper-soak activation a routine deployment step, not another architecture/product gate.

## What is deliberately deferred until after soak start

Unless new runtime evidence shows a blocker, do **not** delay the experiment for:

- more model benchmarking;
- more agent/prompt intelligence work;
- additional decision-chain redesign;
- richer dashboard charts or visual polish;
- speculative feature expansion.

After paper trading starts, prioritize improvements from observed evidence: positions chosen, specialist disagreement, PM/RM reasoning, deterministic blocks, order/fill behaviour, cost/latency, missed opportunities, attribution, performance and Mission Control usability during real sessions.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign without a new contract.
- No broker-write Mission Control controls.
- No public services.
- Preserve `dev` / `qamc` / `ubuntu` isolation.
- Keep upstream OneCLI as the credential layer.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.
