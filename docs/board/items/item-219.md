**219. The pruning pass reports nowhere the owner looks — OPEN, filed 2026-10-01; the rendering is built, the live confirmation is not. 2026-10-01: the cull itself no longer waits for a full book or a replacement (owner ruling), and the ordering/freshness/anti-churn ruling that followed is recorded as NOT BUILT.** The rotation/pruning pass ran every session and wrote a durable `rotation`/`precheck` row, but the owner saw nothing of it on either surface he actually reads: the Telegram session message said only what the rotation PRE-CHECK concluded, and the dashboard said nothing at all, so a session that examined the whole book and kept all of it was indistinguishable from a session in which the pass never ran. Reporting only; no number that governs a buy, a sell or a size was touched.

DONE WHEN:
  - [x] the session message states that the pass ran and how many holdings it examined, read off the held set the pre-check itself received (`held_examined`), never inferred
  - [x] anything put up to be cut is reported with the CONVICTION reason it was cut on — the entry-bar reasons the holding failed — and never a profit-or-loss one
  - [x] the holdings it KEPT are named as considered and kept, so a silent pass can no longer pass for a pass that never ran
  - [x] every session says whether the score-margin tier is on or off, so the owner is never told the desk pruned more thoroughly than it did
  - [x] the dashboard renders the SAME sentences from the SAME durable row via the run detail, with no second reporting path invented
  - [ ] a real session's stored report is read back and shown carrying the block, on both surfaces, against a run the desk actually made — until then this is rendering proven only by test
detail: docs/board_notes/item-219.md


**Retired item numbers — never reuse.** APPEND-ONLY as of 2026-09-30 — closing an item adds ONE NEW `- retired <scheme>: N[, N, ...]` line below, in the matching scheme, and never edits an existing line; the running lists used to live on this one physical line, and even the merge driver's own union rule (`scripts/resolve_doc_conflict.py::merge_retired`) could not save it, because GitHub's own squash-merge — what actually runs when a pull request merges on GitHub.com — never invokes a local git merge driver at all. Two closures now append two different lines and merge with no conflict, by construction; no driver needed for this part. **This still takes the NUMBER ONLY — never a reason.** Every retirement's reason lives in `docs/INCIDENT_HISTORY.md`, which is append-only and merges entry-by-entry the same way. `tests/test_status_board.py` fails a change that adds a reason to any line below, or that edits an existing line instead of appending a new one. The per-item reasons this line used to carry were moved to `docs/INCIDENT_HISTORY.md` on 2026-09-26, verbatim, losing nothing. Gate item 7 was moved, not closed: it is item 76. The two numbering schemes are separate — 3 is retired in BOTH, 20 is live here, and 40, 67 and 200 never existed [verified 2026-09-18 against this file's full git history]. Residue of items 100 and 103 lives in items 106 and 115; item 89 was SHRUNK, not retired. The §11.2 ladder stays; the ladder's own unmeasurable-drawdown behaviour is a separate live question. Run `scripts/next_board_number.py` for the next free number — it reads every line below, the live board, and open pull requests; never eyeball this list. It FAILS CLOSED as of 2026-09-30: if the open-pull-request read fails for any reason it exits non-zero and prints no number at all, because it used to print a warning and a number anyway and two pull requests both claimed item 192 that way. Treat a non-zero exit as a hard stop, not a prompt to guess; `--accept-unchecked-number` is the deliberate offline opt-out and labels its answer UNCHECKED.

- retired queue: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 56, 57, 58, 59, 60, 61, 62, 65, 66, 68, 69, 71, 72, 73, 74, 79, 80, 81, 82, 83, 84, 85, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 100, 101, 102, 103, 104, 105, 106, 108, 110, 111, 113, 114, 115, 116, 117, 118, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 148, 149, 150, 151, 153, 154, 155, 156, 158, 159, 160, 161, 162, 164, 165, 166, 167, 168, 169, 170, 171, 172, 175, 176, 178, 179, 180, 181, 184, 189
- retired gate: 1, 2, 3, 4, 5, 6, 7, 8
- retired queue: 64
- retired queue: 191
- retired queue: 163
- retired queue: 86, 173
- retired queue: 198
- retired queue: 112
- retired queue: 77
- retired queue: 152
- retired queue: 183
- retired queue: 197
- retired queue: 18
- retired queue: 192
- retired queue: 147
- retired queue: 182
- retired queue: 195
- retired queue: 109
- retired queue: 19
- retired queue: 196
- retired queue: 99
- retired queue: 157
- retired queue: 119
- retired queue: 193
- retired queue: 107
- retired queue: 185
- retired queue: 76
- retired queue: 214
- retired queue: 199
- retired queue: 212
- retired queue: 174
- retired queue: 20
- retired queue: 216
- retired queue: 217
- retired queue: 194
- retired queue: 200
- retired queue: 221
- retired queue: 223
- retired queue: 222
- retired queue: 215
- retired queue: 220
- retired queue: 190
- retired queue: 211
- retired queue: 17
