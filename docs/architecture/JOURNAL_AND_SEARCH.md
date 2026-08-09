# Journal & Search Architecture

## Canonical-data rule
The journal is a human-readable projection of quant-agent's existing decisions/trades/reflection/history. It is not another memory system.

## Views
- calendar;
- chronological list;
- structured daily page with previous/next navigation.

## Daily sections
1. Market Thesis
2. Watchlist / Candidates
3. Agent Analysis
4. Disagreements
5. Portfolio Manager Proposal
6. Risk Review — AI Risk + deterministic gate
7. Proposed → Executed Difference
8. Trades
9. Daily Results
10. Lessons Learned
11. Tomorrow — watch items/risks/regime
12. Inspect Agent Calls — the `agent_logs` rows for that day's `run_id`s
    (full prompt, full response, model, tokens, cost), with
    `scripts/replay_decision.py` as the offline re-execution path.
    *(Was "Inspect AI Trace when a trace exists"; re-scoped 2026-08-09 when
    AgentLens was dropped — DECISION #34/#35. There is no external trace
    service.)*

## Search
Initial target: server-side indexed structured/full-text search over a rebuildable read model. Prefer existing/local storage and SQLite FTS5 if compatible after Stage 0.

Structured dimensions: symbol, date, agent, provider/model, decision, confidence, proposed/executed action, rejection/risk intervention, prompt version, Meta change, outcome/P&L, tags/section, reasoning text, `run_id`.
(`run_id` replaces the former "trace ID" dimension — it is quant-agent's own
correlation key, already shared by `agent_logs` and `trades`. DECISION #35.)

Later: natural language translates to a validated filter object and displays the generated filters. No arbitrary model-generated SQL.

## Suggested Investigations
Later deterministic templates may surface high-confidence losses, PM-vs-risk reductions, agent disagreements, post-prompt-change outcomes, expensive no-trade deliberations and profitable opportunities rejected by hard risk.
