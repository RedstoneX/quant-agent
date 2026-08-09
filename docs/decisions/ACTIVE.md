# QAMC Active Decisions

This file contains the decisions that are currently operative. The previous numbered decision ledger is preserved at
`docs/history/legacy/DECISIONS_2026-08-09.md`; references such as “DECISION #34” refer to that historical ledger.

## Product and experiment

1. QAMC is a private, noncommercial experiment around `yebof/quant-agent`.
2. The experimental question is whether inexpensive modern AI models add measurable out-of-sample trading value beyond deterministic signals.
3. `yebof/quant-agent` remains the authoritative trading engine; QAMC surrounds it rather than replacing it.
4. Alpaca Paper is the broker boundary. Live trading is not authorized.
5. AI handles judgment; deterministic Python risk/execution and broker protection own final safety/eligibility.
6. Existing quant-agent memory, reflection, and Meta Reflector remain authoritative learning mechanisms.

## Provider and attribution

7. Per-agent provider/model selection belongs in the backend configuration, not the frontend trading path.
8. Requested versus actual provider/model, fallback, token/cost/latency/status, prompt version, and decision correlation must remain analyzable.
9. Fallback may improve resilience but must never silently contaminate experimental attribution.

## Mission Control

10. Mission Control is a QAMC-native React/Vite/Tailwind application.
11. OpenTradex is a selective trading-dashboard presentation/layout donor; its backend/trading assumptions are discarded.
12. Orallexa is a selective multi-agent UI/concept donor; adapt, do not vendor its trading backend.
13. TradingView Lightweight Charts is the financial charting foundation.
14. The accepted Stage-2 API is read-only, separate-process, and non-critical to trading.
15. Mission Control cannot directly place/cancel/modify/bypass broker orders during the current read-only phases.
16. Journal/search are projections/indexes over canonical QAMC records, rebuildable and non-authoritative.
17. AgentLens is removed from the plan; native `agent_logs` + `run_id`/`decision_id` + replay provide forensic observability.

## Runtime and infrastructure

18. One primary QAMC repository.
19. Permanent runtime target is Linux VPS/server; Claude Code Cloud is development, not runtime.
20. Initial private remote access should prefer Tailscale/private networking.
21. `here.now` may be used for preview/staging only.
22. SQLite/local existing storage is preferred; no distributed infrastructure without demonstrated accepted need.
23. Preserve upstream mergeability and avoid gratuitous trading-core rewrites.

## AI engineering operating model

24. Claude Code is the engineering lead/orchestrator for authorized implementation; the operator sets product/experiment intent and accepts checkpoints.
25. ChatGPT is the independent architecture challenger/checkpoint reviewer and GitHub governance/integration layer.
26. Delegate outcomes, not implementation recipes. Claude owns routine decomposition, worker topology, integration, and testing inside accepted boundaries.
27. Use progressive disclosure: short always-on instructions, path-scoped rules, on-demand skills, and isolated workers instead of loading a governance packet into every session.
28. Cost-aware delegation order: lead context → focused inexpensive subagent → stronger isolated worker/worktree → full agent team only when peer coordination materially helps.
29. Agent teams are optional/experimental accelerators, never a project dependency.
30. Git is the durable project memory. Project auto-memory is disabled to avoid hidden environment-specific state.
31. Usage-window exhaustion and context-window health are separate. Resume coherent sessions after usage reset; split/compact when context itself becomes unhealthy.
32. External checkpoints remain independent: Claude self-reviews and pushes; ChatGPT/operator review before merge.
33. Current exception: Stages 3–5 are one authorized engineering tranche with internal Stage-3/4 gates and external STOP after Stage 5 / Checkpoint E.

## Future live architecture

34. Future live operation is conditional on demonstrated paper profitability and a later explicit authorization.
35. The future architecture may include a separate small **Sentinel** VPS over Tailscale to independently monitor heartbeat, broker positions, and engine health and to provide an external stop/override path. This is architecture-only now; do not implement it in the current tranche.
