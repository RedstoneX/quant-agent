## item 216 — the short-side gap haircut has two application sites

Filed 2026-09-30 out of the item 186 pass. Execution sizes a position as
min(qty_by_alloc, qty_by_risk). The constructor applies the short-side
haircut on the allocation leg; the risk-budget leg in
`src/pipeline_stages.py` reads `RiskConfig.short_gap_risk_multiple` and
applies it there too. Whenever the risk leg is the binding one — which is
whenever risk is the tighter constraint, not an exotic case — the number
that actually sizes the live short is the execution-side read.

How it surfaced: a constructor-only rewrite of the haircut was about to ship
a number-ledger line stating the value "NO LONGER SIZES ANY SHORT". Checked
against the code, that was false. The rewrite stood down; this item records
the split that made the false claim possible.

Severity: LATENT, not live-breaking. Both sites hold the same value today,
so they agree — by coincidence of configuration, not by construction. The
defect is that a change to one site is silently a partial change, and that
any claim about "the" short haircut is ambiguous about which site it means.

Not to be conflated with item 186, which is about whether the VALUE is
sourced. This item is about WHERE it is applied and would remain open even
if the value were settled tomorrow.

