## item 222 — RETIRED 2026-10-01, only two of the three limits are independent; the 65% single-name notional ceiling is the binding one, measured on real orders

**Plain language --** Three different limits each claim to bound how much of one name the desk may hold, and nobody has reconciled them: a 65% single-name ceiling, a 5% per-position risk envelope, and a separate notional cap that looks like the one actually stopping real orders. They are expressed in different units against different denominators, so reading the config cannot tell you which one governs. Until that is settled, nobody can answer the simplest live-money question there is -- how much of one stock can this desk own.

**Why it is filed separately --** Surfaced by board item 90's tranche-four routing pass over the portfolio constructor's sizing object and deliberately not fixed there, because routing a number's provenance is not the same job as deciding which limit binds.

**DONE WHEN, all falsifiable:**
- The binding constraint is identified FROM REAL ORDERS -- the recorded order and refusal stream showing, for each order, which of the three limits was the first to bind -- and NOT from reading the config or the prose.
- A single written statement says, in one sentence, what the maximum holding in one name is and in which unit, with the other two limits shown to be slack or shown to bind first in named circumstances.
- Each of the three limits is reconciled into the same unit against the same denominator, or one of them is deleted, and the ledger rows for all three state the same answer as that statement.
- A test fails if the three limits are ever changed into a combination where which one binds is again unreadable.


---

## THE ANSWER (2026-10-01)

**One sentence, the canonical form of which lives in code as
`SINGLE_NAME_BINDING_SENTENCE` in `src/portfolio_constructor.py` so the
portfolio manager's sector-preview prompt can point at it:**

> The most the desk may hold in one name is 65% of total account equity in
> notional terms (raw position value / equity, before the gross multiplier,
> less whatever is already held in that name); the 5% per-position risk
> envelope and the constructor's notional risk cap are one limit expressed in
> two units, not two limits, and that limit binds before the 65% ceiling only
> when the stop sits further than 7.69% of entry price away.

**There were never three independent limits.** The constructor's
`alloc_cap_by_risk` is computed as `risk_budget_pct x entry / |entry - stop|`
— the 5% per-position risk envelope converted into notional units so it can
be compared against the notional ceiling. It cannot bind an order the
envelope would not. This is the "limit that only looks like a separate bound"
case: it reduces algebraically to another limit.

**The two that remain already share a unit and a denominator** — percent of
`total_value` (total account equity), raw notional, before the gross
multiplier. Nothing had to be converted and no conversion had to be invented.
They cross exactly once, at a stop distance of
`risk_budget_pct / max_position_pct x 100` = 5 / 65 x 100 = **7.69% of entry
price**. Tighter stop: the 65% ceiling binds. Wider stop: the 5% envelope
binds. Derived by rearrangement, not chosen.

**No value changed and no trade that was allowed became disallowed, or vice
versa.** The reconciliation is in units, prose and the ledger only.

## CRITERION 1 — attribution FROM REAL ORDERS, not from the config

Read READ-ONLY from the production database, `trades` table,
2026-09-02 to 2026-09-30 (the full extent of the recorded order stream).

| | count |
|---|---|
| entry orders recorded (BUY / SHORT / SWEEP_BUY) | 46 |
| **attributable** — constructor-sized, carry a stop and a risk pct | **38** |
| **not attributable** — cash-sweep ETF buys, no stop, not constructor-sized | **8** |
| bound by the 65% single-name notional ceiling | **4 of 38** |
| bound by the 5% per-position risk envelope | **0 of 38** |
| bound by the risk envelope's notional image (`alloc_cap_by_risk`) | **0 of 38** |
| scaled by the sector dial (not one of the three) | 2 |

The four ceiling-bound orders each carry the constructor's own clamp note in
their recorded reasoning, naming the ceiling — so the attribution is read off
the record, not inferred. Every one of them had a stop distance tighter than
the crossover (6.33%, 3.05%, 2.82%, 2.61% of entry), which is what the
arithmetic predicts. The earliest of the four was clamped at the then-20%
ceiling, before the 2026-09-11 move to 65.

The risk envelope never bound because **no order ever requested more than
3.0% risk** — `requested_risk_pct` equals `allocated_risk_pct` on all 38, and
the maximum requested was 3.0 against an envelope of 5.

**What the record CANNOT say, reported as a finding rather than papered
over:** the `trade_refusals` table is **EMPTY — 0 rows**. No refusal in the
entire production history can be attributed to any limit, because none was
ever recorded. The attribution above therefore covers CLAMPED orders only.
A trade dropped outright by the risk engine's hard block on
`max_position_pct` would leave no row to attribute. Filling that table is a
separate job; it does not change the answer above, because the constructor
sizes UNDER the hard block precisely so the engine never has to drop an
order, and the clamp notes record each time it did so.

## CRITERION 3 — one unit, one denominator

Both surviving bounds are already percent-of-equity notional against
`total_value`; the risk envelope's conversion into that unit is
`alloc_cap_by_risk` and is now commented as a unit conversion rather than a
bound. All three ledger rows
(`src.config.RiskConfig.max_position_risk_pct`,
`src.portfolio_constructor.config.ConstructorConfig.risk_budget_pct`,
`src.portfolio_constructor.config.ConstructorConfig.max_position_pct`) now state the
sentence above. Two false claims were removed from them while doing it:

- The two risk-envelope rows carried "a 20%-notional cap has been limiting
  real trades to ~1%". That described the retired 20, moved to 100 then 33
  then 65 in September; it has been wrong since 2026-09-04.
- The ceiling row's cost line said "this is the cap that actually binds most
  real trades". Measured: 4 of 38. Not most.

Nothing was deleted: the 5% envelope still clamps a PM request above 5% even
though no request has yet reached it, and deleting it would be a behaviour
change this item is not licensed to make.

## CRITERION 4 — the readability guard

`tests/test_single_name_binding_limit.py`, six tests. It fails if:

- a third upper-bound clamp is added to `allocation_pct` in `_build_buy` or
  `_build_short` (AST check against an explicit allow-list of two);
- `alloc_cap_by_risk` stops being the risk envelope in notional units, or
  starts reading the notional ceiling, which would fuse the two bounds;
- either configured value moves without the written sentence and the computed
  crossover moving with it;
- the constructor default drifts from `config/settings.yaml`;
- any of the three ledger rows stops stating the same answer.
