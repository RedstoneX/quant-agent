# Work that can ONLY be settled while the market is open

Filed 2026-10-04 at the owner's request. Several findings stalled tonight not
because they were hard but because the exchange was shut, and they were
scattered across the board, the parked-defect list and memory. This is the
one place they live.

**The distinction that matters.** Two different things get called "needs a
trading day":

- **SANDBOX-ONLY** — settled on the disposable paper account during regular
  hours. These do NOT need the desk switched on and do NOT need the owner's
  permission to trade. They can be done at any open, including an open where
  the desk stays off.
- **NEEDS THE DESK RUNNING** — only the desk's own live behaviour can answer
  it. These wait on the owner's decision to turn it on.

Keep that split. Treating a sandbox question as "blocked on the desk" is how
three of these sat unanswered for weeks.

Rules that still apply at the open: the sandbox is account-disposable and may
be freely broken, but the desk's own paper account must never be touched;
leave the sandbox with zero open orders and zero positions; never commit an
account id, key or token — this repo is public.

---

## SANDBOX-ONLY — do these at the next regular open

### 1. Does shrinking a stop's quantity release the shares for a sell? (TOP)
The load-bearing unknown behind a confirmed unprotected window. Every partial
sell cancels ALL resting protection, sells, then re-places a stop on the
remainder — so the shares being KEPT have no stop for that whole window, and
the code's own docstring says the residual "rides naked". The cancel exists
for a real reason: the broker reserves the shares behind a resting stop and
rejects a sell of them.

If an in-place amend that REDUCES the stop's quantity frees the difference to
sell, the window closes for the entire class and no cancel is needed.

Measured 2026-10-04 and NOT answered: every amend was refused
`42210000 "cannot replace order in accepted status"` — including a price-only
amend on the same order — because the market was shut.

DONE WHEN:
- [ ] A reduced-quantity amend on a resting stop is attempted at the open and
      the broker's answer recorded verbatim.
- [ ] If accepted: a sell of the released shares is attempted and the result
      recorded, settling whether shrink-then-sell works end to end.

### 2. Confirm the in-place price amend still works during open hours
The desk's whole stop-repair design rests on the broker amending in place.
That was measured once, on 2026-09-30. The sandbox's order history still
carries one `replaced` order corroborating it, but it has not been re-run.
A design resting on a single old measurement is worth re-confirming cheaply.

DONE WHEN:
- [ ] A price-only amend on a plain stop-market order succeeds at the open.

### 3. Out-of-hours amend fallback — measure the blast radius
Discovered 2026-10-04: outside regular hours the atomic amend CANNOT RUN AT
ALL, so every overnight and pre-open stop change falls back to
cancel-and-resubmit — the exact mechanism recorded as the root cause of this
desk's stop failures. How often the desk does this, and how long each window
lasts, is unmeasured.

DONE WHEN:
- [ ] The number of stop changes the desk attempts outside regular hours is
      counted from its own records.
- [ ] The cancel-to-replace gap is timed against the sandbox at a real open.

### 4. Partial fills driven by the live stream
The hermetic test covers partial fills by faking fill quantities. It does not
cover fills arriving on the broker's live stream, which is how they actually
arrive. Shorts are also uncovered.

DONE WHEN:
- [ ] A part-filled order on the sandbox, observed through the live stream,
      ends with exactly one stop sized to the shares actually held.
- [ ] The same proven for a short position.

---

## NEEDS THE DESK RUNNING — these wait on the owner

These were each judged to need a real session; a replay cannot close them,
because a replay stamps its own run id over answers recorded earlier and is
therefore not an observation of the desk.

- **Item 78** — delete the blank-falsifier isolate once the seats agree.
- **Item 187** — the chronic economic-data fetch deadline.
- **Item 201** — the remainder of the cancel-and-resubmit stop path.
- **Item 219** — the pruning pass reports nowhere the owner looks.
- **Item 226** — a payment refusal retried as if it were an outage.
- **Item 227** — a seat's read carried no record of when, or in which run.

Two more need production data rather than a live session, so they are NOT
blocked on the desk being on — only a session with production read access can
settle them:

- **Item 218** — whether any NULL evidence row is a FIRST entry (a genuine
  loss) rather than an add to a name whose row predates the column.
- **Item 90** — the settling data behind the unsourced trade-governing
  numbers.
