## item 221 — the sector preview's flat size

Filed 2026-10-01 from item 90's second routing tranche, which found the defect and deliberately did not fix it so it would not be lost in a routing pass.

**Plain language —** Before the portfolio manager decides what to buy, it is shown a preview of what each sector would weigh if the candidates were bought. That preview assumes every candidate gets the same slice of the book. The machinery that actually places the orders does not: it gives each name a size worked out from how far away its stop sits, so a jumpy name with a wide stop gets a small position and a quiet name with a tight stop gets a large one. The two can differ by several times over on the same name.

**Why it governs money —** The manager uses the preview to decide it is too heavy in a sector and to trim, drop or reorder names. It is therefore correcting a portfolio that will never exist, and the correction lands on the real one. Both directions are live: a sector the preview shows as crowded may be light once the real sizes are applied, so a good name is dropped for nothing; a sector the preview shows as comfortable may be heavy, so the crowding the manager was asked to watch for goes through unflagged.

**Not fixed here, deliberately —** The routing pass changes no values and no behaviour. The fix is a real behavioural change to what the manager sees, and it needs its own test evidence; the DONE WHEN criteria in `docs/WORK.md` item 221 are written to be falsifiable, including a test that fails if the preview's size for a candidate is independent of that candidate's stop distance.

