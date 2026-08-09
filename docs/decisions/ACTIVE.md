# QAMC Active Decisions

This file contains the decisions that are currently operative. Historical numbered decisions are preserved under `docs/history/legacy/`.

## Product and experiment

1. QAMC is a private, noncommercial experiment around `yebof/quant-agent`.
2. The experimental question is whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic signals.
3. `yebof/quant-agent` remains the authoritative trading engine; QAMC surrounds it rather than replacing it.
4. Alpaca Paper is the broker boundary. Live trading is not authorized.
5. AI handles judgment; deterministic Python risk/execution and broker protection own final safety/eligibility.
6. Existing quant-agent memory, reflection, and Meta Reflector remain authoritative learning mechanisms.

## Provider and attribution

7. Per-agent provider/model selection belongs in backend configuration, not the frontend trading path.
8. Requested versus actual provider/model, fallback, token/cost/latency/status, prompt version, and decision correlation must remain analyzable.
9. Fallback may improve resilience but must never silently contaminate experimental attribution.

## Mission Control

10. Mission Control is currently intended as a QAMC-native browser/iPad experience; exact frontend architecture remains challengeable during discovery.
11. OpenTradex and Orallexa are donor candidates, not mandatory architecture. Reuse only when adaptation is actually cheaper/better than native work.
12. TradingView Lightweight Charts is the current financial-charting candidate and may be challenged if repository discovery finds a materially better fit.
13. The accepted Stage-2 API is read-only, separate-process, and non-critical to trading.
14. Mission Control cannot directly place/cancel/modify/bypass broker orders during the current read-only phases.
15. Journal/search are projections/indexes over canonical QAMC records, rebuildable and non-authoritative.
16. AgentLens is removed from the plan; native `agent_logs` + `run_id`/`decision_id` + replay provide forensic observability.

## Runtime and infrastructure

17. One primary QAMC repository.
18. Permanent runtime target is Linux VPS/server; Claude Code Cloud is development, not runtime.
19. Initial private remote access should prefer Tailscale/private networking.
20. `here.now` may be used for preview/staging only.
21. SQLite/local existing storage is preferred; no distributed infrastructure without demonstrated accepted need.
22. Preserve upstream mergeability and avoid gratuitous trading-core rewrites.

## AI engineering operating model

23. The operator defines desired outcomes, product preferences/value trade-offs, and final acceptance. The operator is not expected to make routine technical/architecture decisions.
24. Claude Code is an engineering **and architecture participant**. For substantial new work it must first explore the actual repository and challenge the proposed plan against `docs/OUTCOME.md` rather than assuming documented implementation choices are correct.
25. ChatGPT is the independent architecture challenger/reconciliation layer, checkpoint reviewer, and accepted GitHub governance/integration layer.
26. GitHub is the durable shared memory and handoff between Claude sessions and between Claude and ChatGPT.
27. Substantial work uses two cognitive phases: **discovery/architecture challenge** and **implementation**. Discovery is not product implementation.
28. `docs/work/ACTIVE.md` is the single current durable work contract. Git history preserves prior versions; do not accumulate competing active briefs.
29. During discovery, Claude routes unknowns by type:
    - repository facts → investigate;
    - routine engineering choices → decide;
    - genuine operator product/value choices → ask the operator **one at a time**;
    - material architecture/safety/governance issues → record in GitHub for ChatGPT reconciliation.
30. Claude should not ask the operator questions that source inspection, tooling, or engineering judgment can resolve.
31. Discovery ends with an evidence-based KEEP / CHANGE / REMOVE / ADD proposal and a capability/constraint/acceptance contract—not a file-by-file implementation recipe.
32. Discovery must STOP after push for independent ChatGPT/operator reconciliation. No substantial implementation begins from an unaccepted discovery contract.
33. After discovery/reconciliation is accepted and merged, implementation starts in a **fresh Claude Code session** from GitHub so the implementation context does not carry the exploratory interview/transcript burden.
34. During accepted implementation Claude becomes the engineering lead/orchestrator and owns routine decomposition, worker topology, integration, testing, debugging and implementation decisions inside the accepted contract.
35. Delegate outcomes, not implementation recipes. Use progressive disclosure: short always-on instructions, path-scoped rules, on-demand skills, and isolated workers rather than loading a governance packet into every session.
36. Cost-aware delegation order: lead context → focused inexpensive subagent → stronger isolated worker/worktree → full agent team only when peer coordination materially helps.
37. Agent teams are optional/experimental accelerators, never a project dependency.
38. Git is durable project memory. Project auto-memory is disabled to avoid hidden environment-specific state.
39. Repository settings deny secret-bearing environment/credential reads and writes, disable bypass/auto permission modes, enable sandboxed Bash with secret-path denial, and pre-authorize only a small exact set of routine safe checks.
40. Usage-window exhaustion and context-window health are separate. Resume coherent sessions after usage reset; split/compact when context itself becomes unhealthy.
41. External checkpoints remain independent: Claude self-reviews and pushes; ChatGPT/operator review before merge.

## Current Mission Control status

42. The previously authorized Stage 3–5 implementation tranche is **held for Discovery R1**. Its desired capabilities remain candidate product scope, but architecture/sequencing is provisional until Claude's independent discovery is reconciled and accepted.
43. Current authorization is discovery/architecture challenge only, as stated in `docs/STATE.md` and `docs/work/ACTIVE.md`.

## Future live architecture

44. Future live operation is conditional on demonstrated paper profitability and a later explicit authorization.
45. The future architecture may include a separate small **Sentinel** VPS over Tailscale to independently monitor heartbeat, broker positions, and engine health and provide an external stop/override path. This is architecture-only now; do not implement it during current work.
