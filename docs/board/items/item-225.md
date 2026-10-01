**225. The ledger's file-and-line citations are only checked for existing, so they rot silently and read as verified -- OPEN, filed 2026-10-01.** The guard confirms the line exists, not that it still holds what the row says it holds; 247 rows' worth of citations are exposed, 55 into the pipeline files, and at least three are already wrong by hand-check -- and the coming split of the two largest files will move thousands of lines.

DONE WHEN:
- [ ] a test that fails on today's guard proves it now REJECTS a citation whose cited text has changed or moved
- [ ] every citation either carries the text it points at in a form the guard can compare, or is stated as a symbol the guard resolves, so no citation is unverifiable by construction
- [ ] the three citations named in the note are corrected and the guard passes with no exemption list
detail: docs/board_notes/item-225.md
