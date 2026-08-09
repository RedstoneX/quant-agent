# QAMC Decision Register

Status: **architecture baseline frozen; Stage 0 accepted 2026-08-09;
implementation authorized only for Stage 0.5.**

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
20. Journal requirements: calendar, list, daily structured page, thesis, candidates, agent analysis/disagreement, PM proposal, risk review, proposed→executed delta, trades, results, lessons, tomorrow and agent-call drill-down (`agent_logs` by `run_id`; see #35).
21. Search evolves from indexed structured/full-text search to visible natural-language→structured filters; no arbitrary LLM-generated SQL.
22. Suggested Investigations is a desired enhancement, initially deterministic/template-driven.
23. ~~AgentLens is optional sidecar observability only. AgentLens failure must never affect trading.~~ **SUPERSEDED by #34 (2026-08-09).**
24. ~~**Do not fork/upgrade AgentLens initially.** First pilot upstream/as-is with QAMC-side redaction and trace linking. Project/workspace and major FTS changes are deferred until value is demonstrated.~~ **SUPERSEDED by #34 (2026-08-09).**
25. ~~If AgentLens proves valuable, a separate controlled AgentLens fork may later add project/workspace, indexed trace search and generic improvements.~~ **SUPERSEDED by #34 (2026-08-09).**
26. One primary repository for QAMC; no separate Mission Control repository.
27. Permanent runtime: Linux server/VPS using upstream's systemd-oriented operational model where practical.
28. Claude Code cloud may be the primary development environment; it is not the permanent application host.
29. Mission Control is browser/iPad accessible. Initial private remote access should prefer Tailscale/private networking.
30. `here.now` may be used for frontend preview/staging, not as a safety-critical trading dependency.
31. No unnecessary infrastructure. SQLite/local existing storage is preferred until evidence requires otherwise.
32. Licensing is recordkeeping rather than a major architecture selection variable for this private noncommercial experiment; preserve notices/attribution and do not copy unlicensed code.
33. Optional features have an engineering-effort cap: defer/drop them rather than allowing QAMC to become a bespoke platform.

---

## Decisions accepted at Stage 0 sign-off (2026-08-09)

34. **AgentLens is DROPPED from QAMC. Supersedes #23, #24 and #25.**
    Evaluated at `tranhoangtu-it/agentlens` @ `21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f`
    (MIT) and removed from the roadmap — dropped, **not deferred**. Stage 6 is
    retired; no stage depends on it. Grounds: architectural mismatch (deep-trace
    tooling vs. nine flat single-shot calls), near-total overlap with
    `agent_logs` + `run_id` + `scripts/replay_decision.py`, search weaker than
    the Stage 5 native index, no project/workspace dimension, all remaining work
    QAMC-side (manual spans into `_execute()`; no SDK redaction), dormant
    single-author upstream, and a new server-side LLM-key secret surface. The
    tool is well built and genuinely non-blocking; the objection is fit and
    cost. **Reconsideration condition, preserved:** revisit only if QAMC's
    architecture evolves toward deeper or tool-calling agent traces. Record:
    `docs/architecture/AGENTLENS.md`.
35. **Forensic observability is served by native quant-agent records.**
    `agent_logs` (full prompt + full response + model/tokens/cost), `run_id` as
    the correlation key across `agent_logs` and `trades`, `src/replay.py` /
    `scripts/replay_decision.py` for replay, and the Stage 5 indexed search.
    No external observability service is a QAMC dependency. The `TraceLink`
    component becomes `AgentLogLink`, and the journal's "Inspect AI Trace"
    section becomes "Inspect Agent Calls" over `agent_logs`.
36. **Stage 0 / Checkpoint A is ACCEPTED and DONE**, and **Stage 0.5 (the D-1
    actual-model attribution hotfix) is AUTHORIZED** as the next bounded
    implementation stage. Stage 1 stays BLOCKED until Checkpoint A5 is accepted.
    Stage 0.5 is scoped to the nine `insert_agent_log(...)` call sites plus
    targeted tests; it must not touch `base.py:_execute()`, the database schema,
    provider routing or trading behavior.

---

## Stage 0 conflict register

`DOCUMENTATION_GOVERNANCE.md` requires that document/source conflicts be
recorded here rather than silently resolved. Stage 0 found the following. Full
evidence in `docs/STAGE0_BASELINE_AUDIT.md` §9.

Status at Stage 0 sign-off (2026-08-09): **D-2 and D-3 resolved**; **D-1
scheduled as authorized Stage 0.5**; D-4 … D-10 remain open and unassigned.

- **D-1 (conflicts with decision 12).** Decision 12 requires that no fallback
  be silently counted as the requested model. Verified source: all nine
  `insert_agent_log(...)` sites persist `config.llm.<agent>_model` (requested),
  while `cost_usd` is computed from the model that actually answered.
  `AgentResult.model` — the actual model — is never persisted. On a
  cross-provider failover the stored record is internally inconsistent. Also
  conflicts with `MODEL_PROVIDER_ARCHITECTURE.md` "Required contract" and
  `ACCEPTANCE_CRITERIA.md`.
  **Operator decision 2026-08-09 — AUTHORIZED as Stage 0.5** (decision #36).
  D-1 is corrected as a **bounded pre-Stage-1 correctness hotfix**, kept
  separate from the broader Stage 1 provider work. Operator's reason:
  historical experimental attribution cannot reliably be repaired after the
  fact, so correct attribution must exist before new experimental trading data
  is generated. **Not implemented on the Stage 0 branch**; see
  `docs/STAGE0_BASELINE_AUDIT.md` §9A for the exact nine call sites and the two
  limits a hotfix alone does not remove.
- **D-2 — RESOLVED 2026-08-09.** Operator identified Orallexa as
  `alex-jb/orallexa-ai-trading-agent` (MIT); inspected at
  `794a2ec0ce0b1271b468814eee47c2cd4edde147`. Every proposed presentation
  concept was verified to exist except a decision-chain view (absent in both
  donors — remains a native build). It stays an approved donor, **adapted not
  vendored**. Four adaptation costs recorded in `DONOR_COMPONENTS.md`; the
  substantive one is a **naming inversion** — Orallexa's `PortfolioManagerCard`
  carries QAMC's *AI Risk Manager* semantics (approve/reject, scaled position,
  warnings), not its Portfolio Manager's.
- **D-3 — RESOLVED 2026-08-09; DROP ACCEPTED (see decision #34).** Operator
  identified AgentLens as `tranhoangtu-it/agentlens` (MIT); inspected at
  `21ab445a91bf2bc2f8b7eb0a2a8fb70468a9047f`. Stage 0 recommended **DROP FROM
  THE PLAN** and the operator **accepted**; Stage 6 is retired. Nothing was
  integrated or forked. Grounds: architectural mismatch (it explains deep nested traces;
  quant-agent runs nine flat single-shot calls), near-total overlap with
  `agent_logs` + `run_id` + `scripts/replay_decision.py`, weaker search than
  QAMC can build natively (`LIKE` on `agent_name` only, no FTS), no
  project/workspace dimension, all remaining work QAMC-side (manual
  instrumentation into `_execute()`; no redaction in the SDK), dormant
  single-author upstream (69 commits, 1 author, ~4.5 months idle), and a new
  server-side LLM-key secret surface. It *is* genuinely non-blocking and
  operationally light — the objection is fit and cost, not quality.
  Full reasoning: `docs/architecture/AGENTLENS.md` and
  `docs/STAGE0_BASELINE_AUDIT.md` §8B/§8C.
- **D-4 (affects decision 27).** The six per-session systemd units the README
  says the repo ships are absent; only `quant-agent-daily.*` exists, and it
  hardcodes `/home/yebo/quant-agent`.
- **D-5.** `alpaca.base_url` is read nowhere in the codebase; only
  `alpaca.paper` selects paper vs. live. Two knobs are presented where one is
  live, against `ACCEPTANCE_CRITERIA.md` "paper/live configuration cannot be
  casually confused". Current values agree and the environment is paper.
- **D-6.** No `upstream` remote configured, contrary to
  `UPSTREAM_INTEGRATION.md`.
- **D-7 (wording, decision 9).** Decision 9 says "Auto-Evolve disabled
  initially"; live config is `evolution.enabled: true, dry_run: true`, which
  the editor treats as STAGE-ONLY (proposals written to `proposed_edits.json`,
  no prompt file modified). Behaviour matches the decision; the flag name does
  not.
- **D-8.** `.env.example` omits seven documented environment variables that
  Stage 1 will touch.
- **D-9 (amends the safety narrative).** `cash_sweep`'s `SWEEP_BUY` reaches the
  broker outside `_filter_hard_risk_decisions`, deliberately and
  deterministically, and is not LLM-reachable. `SAFETY_BOUNDARIES.md` should
  state this carve-out explicitly rather than imply a universal gate.
- **D-10.** `broker.close_position()` has no caller.
