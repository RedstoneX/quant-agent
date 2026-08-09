# UI Component Map

Exact API shapes are Stage 2 work; this is the product contract.

| Component | Purpose | Source/Donor | Initial mode |
|---|---|---|---|
| AppShell / resizable panes | Cockpit layout | OpenTradex adaptation | Read-only |
| PaperModeBadge | Prevent environment confusion | QAMC native | Read-only |
| AccountSummary | Equity/P&L/buying power | QAMC native/donor styling | Read-only |
| CandidateList | Ranked/watch candidates | QAMC native | Read-only |
| PriceChart | Candles/volume/markers | TradingView Lightweight Charts | Read-only |
| PositionsTable | Current exposure/P&L | donor styling | Read-only |
| OrdersTradesTable | Order/trade history | donor styling | Read-only |
| AgentCard | Role/model/recommendation/confidence/cost | Orallexa concept | Read-only |
| DisagreementPanel | Agent divergence | Orallexa concept | Read-only |
| DecisionChain | PM→Risk→Gate→Execution | QAMC native + donor patterns | Read-only |
| ProposedExecutedDelta | Show what hard risk changed | QAMC native | Read-only |
| SystemHealth | scheduler/API/broker/observability state | OpenTradex pattern | Read-only |
| JournalCalendar/List | Historical navigation | Journal IA | Read-only |
| JournalDay | Structured daily explanation | QAMC native | Read-only |
| SearchFilters | Structured/indexed forensics | QAMC native | Read-only |
| TraceLink | Deep-link to AgentLens | QAMC native | Read-only |
| LearningProposal | Meta report/prompt diff | QAMC native | Later write |
| ModelSelector | Per-agent provider/model choice | QAMC native | Later write |
| OperationalControls | pause/resume/kill | QAMC native | Final stages |
