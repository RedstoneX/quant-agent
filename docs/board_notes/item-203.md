## item 203

**Measured against production 2026-10-01 (`/home/qamc/quant-agent/data/quant_agent.db`, read-only; the repo-local DB is empty).** Across all 713 `agent_logs` rows, 2026-08-14 to 2026-10-01: ZERO rows where a provider request happened (`provider_requests > 0`) and no token counts and no cost came back. The eleven rows carrying no token counts at all are all `provider_requests = 0` -- no request was made -- which is item 147's already-settled cache-hit case. 238 successes report $0 against real token counts, all from the `google` free route: a correctly measured zero, not a hole. So the occurrence count this item was filed to watch for is still zero, and nothing can be priced from an occurrence that has never happened -- the item stays OPEN rather than being closed on a fallback price the desk would have to invent.

**What shipped anyway (2026-10-01): the case is now counted as unknown instead of banked as a measured zero.** `_unknown_cost_row_expr` in `src/cost_circuit.py` scored a row unknown only when `cost_usd IS NULL`. A success whose provider request happened and returned no usage would be written with a literal `cost_usd = 0.0` -- zero because nothing was reported, not because anything was measured -- and that row read to the budget as a proven free call. It now scores 1 (unknown) whenever `telemetry = 'no_usage'`, which `src/agents/base.py:usage_telemetry_word` writes only for a row whose request reached the provider. Disjoint from `_PROVEN_ZERO_ROW_SQL` by construction, and the clause is dropped (not fabricated) on an older `agent_logs` with no `telemetry` column. No price is substituted from a model list, an average of other calls, or anything else: the day simply loses `costs_exact` and the hole stays visible.

**The `telemetry` recorder (#879, merged 2026-09-30) has captured nothing in production yet -- UNPROVEN, not proven dead.** All 713 rows have `telemetry` NULL. The only session since the merge ran 2026-10-01 00:01-00:02 ET while the production checkout's HEAD was being updated at 00:01:13 ET, so no row has yet been written by a process that provably loaded the recorder. Re-check after the next full session: three consecutive successes still NULL would make it dead and a defect in its own right. Until a row carries the word, the clause added above is correct but unexercised in production.

Filed 2026-09-30, carried over from item 147 at retirement. Item 147 measured zero rows in agent_logs where a provider request actually happened and returned no usable cost or token telemetry, so nothing needs building today; this item exists only so that case is tracked if it ever fires, rather than silently dropped when 147 was retired.



## CLOSED 2026-10-02 — the recorder is alive and the watched case has still never occurred

**The `telemetry` recorder is POPULATING, not dead.** Measured on the production
database 2026-10-02 (read-only): 725 `agent_logs` rows, of which 12 carry
`telemetry = 'complete'` and 713 carry NULL. Every one of the 12 was written on
2026-10-01 between 13:31:12 and 14:18:07 ET. The only three rows written after
the #879 merge that are still NULL are ids 711-713, timestamped 00:01:07,
00:01:56 and 00:02:10 ET — inside the exact window this note already predicted,
while the production checkout's HEAD was being updated at 00:01:13. Every row by
a process that provably loaded the recorder carries the word. The re-check this
note asked for has run and the recorder is not a defect.

**The watched case still has zero occurrences.** Of 725 rows, 0 have
`provider_requests > 0` with no token counts. The 11 rows with no token counts
all have `provider_requests = 0` and `latency_s = 0.0` — no request was made.
The counter is real rather than always-zero: `smart_money_analyst` alone shows
18 rows at `provider_requests = 1` against its 11 at 0.

**No day has ever been marked inexact by this case.** All 28 `llm_budget_days`
rows carry `unknown_cost_rows = 0`.

**Why the box is ticked.** The DONE WHEN asked for the case to be understood and
either priced from a fallback or proven free and excluded from unknown-cost
counting. It is understood: it has never happened, and the settlement path in
`src/cost_circuit/parts/settlement.py` books a success whose cost is missing as
`unknown_cost_rows + 1` with `costs_exact = 0`, so the hole stays visible rather
than being banked as a measured zero or filled with an invented price. Nothing
is left to build, and inventing a fallback price for an event with no instances
would be exactly the made-up number the desk bars.
