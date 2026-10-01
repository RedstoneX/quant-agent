# Board notes — owner-facing prose for the desk status board

**This file is NOT the source of truth.** `docs/WORK.md` is: it alone
records what this desk is doing — an item's number, its title, its status,
its ordering. `docs/BOARD_NOTES.md` supplies nothing but the words a trader
reads about each item: a plain-language explanation, a worked example, and
where a ruling is needed, the decision plus a recommendation. If this file
disagrees with `docs/WORK.md` about whether something is open, done, or even
still exists, `docs/WORK.md` wins, always.

**Who writes this file.** The orchestrating assistant, drafting explanation
for the owner in plain English — never the owner, and never a mechanical
process. Nothing here is generated, inferred, or summarised out of
engineering notes. An item with no entry below renders on the board with an
explicit "not yet explained in plain language" marker — that is the honest
state, and it is never papered over with invented prose.

**No size cap.** `docs/WORK.md` is mechanically capped at 100,000 bytes on
purpose (see
`tests/test_status_board.py::test_work_md_stays_under_a_hundred_thousand_bytes`)
because it must stay a short, current, agent-facing backlog — not an
append-only prose archive. This file carries none of that risk. It can grow
without threatening the backlog's cap, so it has no size limit of its own.

`scripts/status_board.py` reads this file for prose and `docs/WORK.md` for
everything else, every time the board is built. See that script's module
docstring ("The prose problem, and the convention that solves it") for the
full mechanical account.

## How an entry is keyed

Each block below is introduced by a heading naming the exact item it
explains, by its NUMBER and SECTION — never by its title. A title can be
reworded in `docs/WORK.md` at any time; a prose block keyed to words would
silently go stale, or orphaned, the moment that happened. Keying on the
number and section survives a rename intact.

  * `## item N` — a funnel-queue item, N being its rank under
    `## THE FUNNEL QUEUE` in `docs/WORK.md`.
  * `## gate item N` — a PM-test-gate item, N being its rank under
    `## PM TEST GATE` in `docs/WORK.md`. The funnel queue and the gate both
    number from 1, which is why "gate item" and "item" are distinct keys —
    see `_SOURCE_REF_LABEL` in `scripts/status_board.py`.
  * `## decision due YYYY-MM-DD` — a pending `- [ ] DECIDE BY ...` line in
    `docs/WORK.md`, keyed by its due date because a decision carries no
    number of its own.

These are exactly the identifiers the board already shows the owner, and
that he quotes back to refer to something ("item 32", "gate item 4",
"decision due 2026-09-16") — see `QueueItem.ref` and `PendingDecision.ref`
in `scripts/status_board.py`. The heading is matched case-insensitively and
with any amount of whitespace, but the words and the number must be there.

Under each heading, each field on its own line, in the same shape
`scripts/status_board.py`'s `parse_prose` has always read:

    **Plain language —** what this is, in words a trader understands.
    **Example —** a concrete case that makes it tangible.
    **The decision —** what the owner specifically has to rule on.
    **Recommendation —** what we think he should do, stated as a
    recommendation.
    **Why only you —** the one reason this decision cannot be made without
    him: money, mandate, risk appetite, or public disclosure. Never a
    market-structure number — those are researched, not decided, and an
    item like that writes `**The decision —** None for you` instead.

The label must start the line, the surrounding `**` is optional, and the
separator may be an em dash, a hyphen or a colon. A block ends at the next
label, the next heading, or a blank line — one paragraph per label. Nothing
is mandatory: an item can carry only a `Plain language` line and nothing
else, and any field left out renders as absent, never guessed.

**"Waiting on you" on the board is built from two fields together.** An
item shows there only when it carries BOTH a live `The decision` (not
starting "None", "Not yet" or "Possibly") AND a `Why only you`. Writing a
decision without a reason, or a reason without a decision, leaves it where
it already was — in the running order, not promoted. See
`_is_live_owner_ask` in `scripts/status_board.py`.

## Worked example — illustrative only, not a real backlog item

The heading below uses the letter `N` in place of a number so it can never
collide with a real item in `docs/WORK.md`. It exists only to show the
shape; delete nothing above this section when adding real entries below it.

```
## item N

**Plain language —** The desk refuses a trade unless the likely gain is at
least one and a half times what it is risking.
**Example —** You want to buy at $100 with a stop at $98 and a target at
$105: risking $2 to make $5. The desk moves the stop to $95 on its own,
recalculates it as risking $5 to make $5, and refuses the trade.
**The decision —** Should a stop you can point at on the chart always be
honoured, however tight it is?
**Recommendation —** Yes. Pad the stop only when there is no real level to
put it at.
```

---

Real entries for the current backlog are drafted separately and go below
this line, one heading per item.

## items 182 / 183 / 185 / 186 — consolidation check against item 90 (2026-09-30)
**Verdict: all four KEPT, none retired.** Each opens with "item 90's half two, surfaced for visibility", but each carries its own DONE WHEN criteria that item 90 does not own: 182 the ladder alert and cash-deficit cushion, 183 the order gates and the dead cash-sweep config removal, 185 the ATR-eligibility question and its two inherited rows, 186 three open owner-appetite answers. Retiring any would lose those criteria. The defect found was in item 90 itself: it claimed the four carry one word-for-word shared criterion, which was false (checked against each block). Item 90's line now names the four tranches and what each covers. No constant, threshold or value was chosen or changed.
## items 182 / 183 / 185 / 186 — consolidation check against item 90 (2026-09-30)
**Verdict: all four KEPT, none retired.** Each opens with "item 90's half two, surfaced for visibility", but each carries its own DONE WHEN criteria that item 90 does not own: 182 the ladder alert and cash-deficit cushion, 183 the order gates and the dead cash-sweep config removal, 185 the ATR-eligibility question and its two inherited rows, 186 three open owner-appetite answers. Retiring any would lose those criteria. The defect found was in item 90 itself: it claimed the four carry one word-for-word shared criterion, which was false (checked against each block). Item 90's line now names the four tranches and what each covers. No constant, threshold or value was chosen or changed.
