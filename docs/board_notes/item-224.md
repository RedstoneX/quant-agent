## item 224

Moved from `docs/WORK.md` on 2026-10-01 to satisfy the per-item note-pointer
check, which landed after this item was filed. VERBATIM -- not a word of the
filing author's prose is changed, and nothing is added to it here.

**224. The desk records no realised sector weights, so concentration can only be guessed before the fact and never read after it -- filed 2026-10-01 from item 221.** Item 221 established that the pre-decision preview cannot project a sector mix at all, because sizing depends on a PM target that does not exist when the preview is built; what the desk could record instead, and does not, is the sector weights of the orders the constructor ACTUALLY built, once per run. Without that row nobody can say afterwards whether a session concentrated the book or not. Detail in `docs/board_notes/item-221.md`.

DONE WHEN:
- [ ] one durable row per run carries the realised `(sector, side)` weights of the orders the constructor built that session, written from executable product code with its call site named, and classified POPULATING rather than UNPROVEN against a real session
