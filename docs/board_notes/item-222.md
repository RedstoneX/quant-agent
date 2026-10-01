## item 222

**2026-10-01, carried in from item 221 —** a candidate's stop-implied ceiling (its risk budget over its own stop distance) is a FURTHER claim on the same quantity, and at this desk's ordinary stop widths it sits several times above the single-name notional ceiling, so which limit binds is settled by how wide the analyst drew the stop rather than by anything the owner ratified. The projected-portfolio preview now clamps the ceiling it prints to the single-name one and tells the PM that which limit binds is unsettled and lives here. Reconcile the limits; do not fix this in isolation.

**Plain language --** Three different limits each claim to bound how much of one name the desk may hold, and nobody has reconciled them: a 65% single-name ceiling, a 5% per-position risk envelope, and a separate notional cap that looks like the one actually stopping real orders. They are expressed in different units against different denominators, so reading the config cannot tell you which one governs. Until that is settled, nobody can answer the simplest live-money question there is -- how much of one stock can this desk own.

**Why it is filed separately --** Surfaced by board item 90's tranche-four routing pass over the portfolio constructor's sizing object and deliberately not fixed there, because routing a number's provenance is not the same job as deciding which limit binds.

**DONE WHEN, all falsifiable:**
- The binding constraint is identified FROM REAL ORDERS -- the recorded order and refusal stream showing, for each order, which of the three limits was the first to bind -- and NOT from reading the config or the prose.
- A single written statement says, in one sentence, what the maximum holding in one name is and in which unit, with the other two limits shown to be slack or shown to bind first in named circumstances.
- Each of the three limits is reconciled into the same unit against the same denominator, or one of them is deleted, and the ledger rows for all three state the same answer as that statement.
- A test fails if the three limits are ever changed into a combination where which one binds is again unreadable.

