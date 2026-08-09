# QAMC Decision Register

Status: **architecture baseline frozen; implementation not started**.

1. Project name: **Quant Agent Mission Control (QAMC)**.
2. `yebof/quant-agent` remains the authoritative trading engine.
3. This repository (`RedstoneX/quant-agent`) is the controlled primary fork; keep upstream mergeability.
4. Broker: **Alpaca Paper**. Live trading is not authorized.
5. Specialist agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution remains the authoritative decision chain.
6. AI handles judgment; deterministic Python and broker protection own final safety/execution eligibility.
7. Risk failure = fail closed.
8. Existing quant-agent memory, evening reflection and Meta Reflector remain authoritative learning mechanisms.
9. Initial Meta-Reflector policy: analyze → propose → human approve/reject. Auto-Evolve disabled initially.
10. Add a backend provider abstraction/OpenRouter capability surgically; do not route models in the frontend.
11. Per-agent model selection is required. Model changes apply at safe invocation boundaries and are logged.
12. No silent provider/model fallback in experimental records. If fallback is retained for resilience, the actual model/provider must be explicit and analyzable.
13. Mission Control is a **QAMC-native React/Vite/Tailwind application**, not an OpenTradex application forced onto quant-agent.
14. OpenTradex is the primary trading-dashboard UX/component donor. Reuse presentation/layout components selectively; discard its trading/gateway/data assumptions.
15. Orallexa is the primary multi-agent trading UI/design donor. Adapt agent cards, disagreement/fusion, PM/risk presentation, model scoreboard and cost concepts selectively.
16. TradingView Lightweight Charts is the financial charting foundation.
17. QuantDinger is visual inspiration only unless a specific component proves clearly cheaper to reuse.
18. Native journal derives from canonical quant-agent data; do not create a second trading-memory system.
19. Derived search/read indexes are rebuildable and non-authoritative.
20. Journal requirements: calendar, list, daily structured page, thesis, candidates, agent analysis/disagreement, PM proposal, risk review, proposed→executed delta, trades, results, lessons, tomorrow and trace drill-down.
21. Search evolves from indexed structured/full-text search to visible natural-language→structured filters; no arbitrary LLM-generated SQL.
22. Suggested Investigations is a desired enhancement, initially deterministic/template-driven.
23. AgentLens is optional sidecar observability only. AgentLens failure must never affect trading.
24. **Do not fork/upgrade AgentLens initially.** First pilot upstream/as-is with QAMC-side redaction and trace linking. Project/workspace and major FTS changes are deferred until value is demonstrated.
25. If AgentLens proves valuable, a separate controlled AgentLens fork may later add project/workspace, indexed trace search and generic improvements.
26. One primary repository for QAMC; no separate Mission Control repository.
27. Permanent runtime: Linux server/VPS using upstream's systemd-oriented operational model where practical.
28. Claude Code cloud may be the primary development environment; it is not the permanent application host.
29. Mission Control is browser/iPad accessible. Initial private remote access should prefer Tailscale/private networking.
30. `here.now` may be used for frontend preview/staging, not as a safety-critical trading dependency.
31. No unnecessary infrastructure. SQLite/local existing storage is preferred until evidence requires otherwise.
32. Licensing is recordkeeping rather than a major architecture selection variable for this private noncommercial experiment; preserve notices/attribution and do not copy unlicensed code.
33. Optional features have an engineering-effort cap: defer/drop them rather than allowing QAMC to become a bespoke platform.
