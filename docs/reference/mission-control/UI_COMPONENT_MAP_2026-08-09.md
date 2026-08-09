# UI Component Map

Exact API shapes are Stage 2 work; this is the product contract.

> **Stage 0 donor-source corrections (2026-08-09).** Donors inspected at pinned
> commits — OpenTradex `30b23f5e`, Orallexa `794a2ec0`. Three rows below needed
> correcting; the `Source/Donor` column now reflects verified source.
> Evidence: `docs/STAGE0_BASELINE_AUDIT.md` §8, §8A.
>
> - **`AgentCard` / `DisagreementPanel`** — both are real, and they are the
>   *same* Orallexa component: `PerspectivePanelCard`
>   (`app/components/scenario-panel.tsx`), which renders consensus + agreement %
>   + a divergence bar + one row per analyst role. It self-fetches
>   `/api/role-memory`; lift that to a prop on adoption.
> - **PM presentation** — Orallexa's `PortfolioManagerCard` carries
>   approve/reject + scaled position + warnings + confidence adjustment. Those
>   are QAMC's **AI Risk Manager** semantics, not its Portfolio Manager's. Wire
>   it to `risk_manager`; QAMC's PM (the proposer) has no donor and is native.
> - **`DecisionChain`** — confirmed **fully native**. Neither donor has a
>   PM→Risk→Gate→Execution view. OpenTradex's `FlowVisualizer` draws *skill
>   graphs*, not decision chains.
> - **`TraceLink` → `AgentLogLink`** — the AgentLens drop was **accepted**
>   (DECISION #34), so there is no external trace service. Re-scoped to
>   deep-link a `run_id` to its `agent_logs` rows (full prompt, full response,
>   model, tokens, cost), with `scripts/replay_decision.py` as the offline
>   re-execution path.
> - **OpenTradex caveat** — its `AgentConsole` / `RunsAuditPanel` present
>   *skill-run* orchestration, not per-analyst recommendations. Its useful
>   donation is layout and visual language.

| Component | Purpose | Source/Donor | Initial mode |
|---|---|---|---|
| AppShell / resizable panes | Cockpit layout | OpenTradex adaptation | Read-only |
| PaperModeBadge | Prevent environment confusion | QAMC native | Read-only |
| AccountSummary | Equity/P&L/buying power | QAMC native/donor styling | Read-only |
| CandidateList | Ranked/watch candidates | QAMC native | Read-only |
| PriceChart | Candles/volume/markers | TradingView Lightweight Charts | Read-only |
| PositionsTable | Current exposure/P&L | donor styling | Read-only |
| OrdersTradesTable | Order/trade history | donor styling | Read-only |
| AgentCard | Role/model/recommendation/confidence/cost | Orallexa `PerspectivePanelCard` (verified) | Read-only |
| DisagreementPanel | Agent divergence | Orallexa `PerspectivePanelCard` — same component | Read-only |
| RiskVerdictCard | AI Risk Manager approve/reject, scaling, warnings | Orallexa `PortfolioManagerCard` (rewired — see note) | Read-only |
| DecisionChain | PM→Risk→Gate→Execution | **QAMC native — no donor exists** | Read-only |
| ProposedExecutedDelta | Show what hard risk changed | QAMC native | Read-only |
| SystemHealth | scheduler/API/broker/observability state | OpenTradex pattern | Read-only |
| JournalCalendar/List | Historical navigation | Journal IA | Read-only |
| JournalDay | Structured daily explanation | QAMC native | Read-only |
| SearchFilters | Structured/indexed forensics | QAMC native | Read-only |
| AgentLogLink | Deep-link a `run_id` to its `agent_logs` rows (prompt/response/model/tokens/cost) | QAMC native — replaces the retired `TraceLink` | Read-only |
| LearningProposal | Meta report/prompt diff | QAMC native | Later write |
| ModelSelector | Per-agent provider/model choice | QAMC native | Later write |
| OperationalControls | pause/resume/kill | QAMC native | Final stages |
