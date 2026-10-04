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

---

## PLAN — pointing a FULL daily session at the disposable paper account

Investigation only, 2026-10-04. Nothing was run. Evidence is from the repo at
origin/main; not exercised against the broker.

**1. Credentials or code?** Credentials, plus a separate checkout. No code
names the account: the account id appears nowhere in the repo; the account is
chosen only by the key pair `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (see
`api_keys` in `config/settings.yaml`). `alpaca.base_url` is already the paper
host and `AlpacaConfig` refuses `paper: false`, so a sandbox key pair on the
same host needs no config edit. The one trap: the key pair, the Telegram
token/chat and the LLM keys all arrive from the same environment or systemd
credentials, so the sandbox keys must be supplied to a process that does NOT
inherit the desk's unit.

**2. Where writes land.** Into whichever checkout the process runs from, NOT
into a database named by config alone. `storage.db_path` is relative
(`data/quant_agent.db`), and the notifier, both watchdogs, the refusal-signature
reader and the route journal each compute their own path from their source
file's location (`Path(__file__)...`), so a second checkout with its own empty
`data/` isolates all of them. Pointing the production checkout at other keys
would NOT isolate: it would write sandbox trades into the production database.
Also under `data/`: news, macro, checkpoints, evolution, and the kill-switch
file. `QUANT_AGENT_DB_PATH` redirects only the route journal. Never run it with
`/home/qamc` as the working directory. The rehearsal rig (`ops/rehearsal`) is
frozen and walls off the network, so it cannot be reused for a live-paper run.

**3. External cost.** One session is capped by `llm_cost_circuit`: 0.90 USD per
session, 2.75 USD per day, 40 calls per session (`config/settings.yaml`). The
cap counts spend in the sandbox's own database, so a fresh checkout starts the
day at zero and the daily cap does NOT see production spend that day. The
spend lands on the same provider accounts as production (same API keys).
Production PM seat is the dominant cost (memory: ~91 percent). Kill switch:
`touch data/KILL_SWITCH` in the sandbox checkout halts every order there; it
does not stop LLM spend.

**4. Evidence supplied.** Of the 8 items in the two lists above:
- 4 sandbox-only items (1 to 4) need no session at all; a session adds nothing.
- 6 "needs the desk running" items (78, 187, 201, 219, 226, 227): a session
  could supply evidence for them only if the triggering condition occurs
  (a payment refusal, a cancel-and-resubmit stop, a pruning pass); [estimate]
  likely 3 to 4 of 6, unverified.
- 2 production-data items (218, 90) stay blocked: an empty sandbox database
  has no history to settle them.
The "about 17 blocked" figure could not be reproduced from this file; the
board was not recounted here.

**5. Not recoverable by resetting the paper account.**
- Telegram: a sandbox session with the desk's bot token messages the owner
  and may be mistaken for real; leave those variables unset.
- LLM spend: real money, up to the caps above, per session.
- Running from the production checkout or `/home/qamc`: contaminates the
  production database and its trade ledger.
- Same key pair as the desk's own paper account: the hands-off rule above is
  broken and that account's orders are cancelled.
- Provider data and news caches written to the wrong `data/` are not undone
  by an account reset.
- A session left running past the open with resting orders.
