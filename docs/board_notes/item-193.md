## item 193 — RETIRED 2026-10-01, the id-ceiling gap is explained: the WAL id sequence is SHARED with the protected-sell exit path, all 15 production cancels carry their own wal_row_id and all pair, and every id handed out is now recorded durably at the single insert choke point

**2026-10-01 — the row-id gap is explained, and the pair count is NOT a floor.**
Measured against the production database (`/home/qamc/quant-agent/data/quant_agent.db`,
read-only). Every `scale_in` / `protective_sell_cancelled` event carries its OWN
`wal_row_id` in its payload, so the cancels do not have to be counted against the
sequence at all: there are 15 of them (2026-09-17..2026-09-30), holding ids 5, 6, 7,
9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20 and 23, and all 15 pair with a later
same-run same-symbol `protection` / `placed` event. `sqlite_sequence` stands at 23 and
`pending_protection_restores` is empty. The eight ids the scale-in path never held are
1, 2, 3, 4, 8, 17, 21 and 22, and the reason the ceiling outruns the cancel count is
structural rather than missing: `pending_protection_restores` is a SHARED autoincrement
and the protected-sell exit path (`TradingPipeline`'s `_WAL_SELL_SENTINEL` write-ahead,
plus the orphan-persist branch) draws ids from the same sequence, as do the scale-in
rollback branches that insert the row and discharge it when the cancel fails or will
not confirm. Six of the eight line up with closing trades: ids 1-4 all predate the
first scale-in cancel entirely and seven sell-side trades precede it, and ids 8 and 17
each fall in a window containing exactly one sell-side trade. Ids 21 and 22 could not
be attributed, and the two honest attempts are written down rather than papered over:
(1) no path except a SUCCESSFUL scale-in cancel files its row id anywhere durable — a
sweep of all 5,836 production `pipeline_event` rows found `wal_row_id` in exactly the
15 scale-in cancel payloads and nowhere else; (2) the production log has rotated past
them — it now reaches back only far enough to show row 23. So the deriving stopped and
the RECORDING was built instead: `protection_restore_wal_audit` takes one never-deleted
row per id, written inside `Database.insert_pending_protection_restore` itself, which is
the single choke point every writer passes through. It carries the writing sentinel, the
symbol, the side and the held quantity, it survives the discharge that deletes the WAL
row, and `get_protection_restore_wal_audit` reads it back, so the next time the ceiling
outruns the cancels the missing ids are named instead of argued about. Bookkeeping only:
nothing rules on it, and a failure to write it never blocks the protective WAL row it
describes (proved by `tests/test_protection_restore_wal_audit.py`). AMENDING IS STILL
NOT THE REMEDY HERE and nothing above reopens it — the 2026-09-30 finding stands: the
cancel exists because the resting protective SELL collides with the BUY add, a price
amend leaves that SELL open, and the quantity amend that would cover an enlarged
position is refused by this broker on a fractional order (42210000).


Measurement only. No production code was written or changed for it; both events already exist.

**The two event names, as they actually appear in the code.** The cancel is filed in `src/pipeline_stages.py` as stage `scale_in`, outcome `protective_sell_cancelled`, reason `cancel_confirmed_via_trade_updates` — the reason string is stale wording kept deliberately, since the `trade_updates` socket has been off since 2026-09-17 and the confirm is now a bounded REST wait. The rearm has NO scale-in-specific event: it is the generic stage `protection`, outcome `placed` or `not_placed`, reason `protective_stop_result`, fired for every entry protection whether or not a scale-in preceded it. Pairing therefore has to be done on `run_id` plus `symbol` plus event ordering, which is why a cancel whose run placed no protection at all would show up as unpaired.

**Method.** Both events land in `specialist_evidence` with `kind='pipeline_event'` (via `_record_pipeline_event` -> `_persist_evidence` -> `Database.insert_specialist_evidence`); there is no `pipeline_events` table. The live file was copied out with its `-wal` and `-shm` sidecars and the counts agreed with and without them. Exposure per event is `abs(held_qty_before)` from the cancel event's own payload multiplied by the add's fill price from the `trades` row for the same run and symbol. Volatility is the standard deviation of the last 20 daily close-to-close log returns strictly before the event date, from daily bars for that name, scaled to the window by `sigma_daily * sqrt(seconds / 23400)`. No volatility number was assumed or carried over from anywhere.

**2026-09-30 — the window is now self-measuring, and amending cannot remove it.**
Each scale-in that cancels a protective stop emits one `scale_in` /
`unprotected_window_closed` event (or `unprotected_window_still_open` when the
rearm did not land) at the moment the rearm attempt returns. Both ends are
`time.monotonic()` readings taken inside the run — the broker's cancel
acknowledgement and the broker's rearm acknowledgement — so the figure never
reflects a row's write time, which was the first DONE WHEN. The event carries
`window_seconds`, the WHOLE `held_qty_before` the cancel exposed (abs()ed, so a
short reads positive), `exposed_notional` (None, never a fabricated 0, when the
reference price is unknowable) and the same `wal_row_id` as the cancel event, so
an unpaired cancel is a visibly missing partner rather than something inferred
from row-id arithmetic. Both new outcome words joined `UNDECIDED_OUTCOMES`:
they are mid-add bookkeeping and rule on nothing.

**2026-09-30 — the skip stays, the silence does not.** The coverage sweep
skips any symbol holding a live scale-in write-ahead row, and that skip is
correct: placing a stop there re-creates the opposite-side block the cancel
just cleared. It also meant the one moment the desk is naked was the one
moment the report said nothing, because a skipped symbol simply vanished from
the sweep. It now appears by name — held quantity, short or long, the row's
`created_at`, and roughly how long protection has been down — in the sweep's
run record, in its single log line, and in `CoverageStatus.unguarded`. It is
kept OUT of `gaps`, because a gap is something the sweep tries to repair and
this one must never be repaired. The duration is the write-ahead row's WRITE
time, not the broker's cancel acknowledgement, so it is labelled approximate
everywhere; the exact figure remains the `unprotected_window_closed` event the
session files at rearm. The overdue test is not a chosen number: the bound is
the LONGEST window the desk has actually measured, read back out of its own
closed-window events, and with no measured history there is no bound and
nothing is called overdue. Over the bound, the owner is paged once per symbol
per trading day on its own footing — never folded into the coverage-gap alert,
which would tell him the desk failed to place a stop it in fact cancelled
deliberately. No broker order is placed by any of this.

**Amending does not close this window.** The desk measured on the rehearsal
account that Alpaca amends a resting stop's price in place, and `broker.py`
grew an amend path. It does not apply here. The cancel exists because a resting
protective SELL holds the shares and collides with the BUY add; a price amend
leaves that SELL open, so the collision — and the reason for the cancel — is
unchanged. The quantity amend that WOULD cover an enlarged position is refused
by this broker on a fractional order (42210000), and scale-in adds are routinely
fractional. The window is a property of the broker's order model, not of the
desk's sequencing, so the remaining work is detection and bounding, not removal.

**2026-09-30 — the lock-held skip is now BOUNDED, which is the only part of
this that was ever removable.** The coverage sweep's skip had two arms. The
crash arm was already safe: with no session lock it skips a symbol only while a
working entry order still rests, so a dead session's naked position is repaired.
The session-lock arm was not: while the wrapper's lock directory existed, every
symbol holding a scale-in write-ahead row was skipped for as long as that row
survived, so a session that cancelled the protective stop and then HUNG without
releasing the lock left the whole held position naked with the one watchdog that
could re-protect it deliberately looking away, with no end. The lock arm now
defers to the desk's own measurement: within the longest window it has ever
closed and recorded — and always when it has measured nothing at all, or cannot
read a row's write time — behaviour is byte-for-byte what it was, because that
is a normal live window. Past that bound the lock stops being reason enough, and
the symbol falls back to the same collision test the crash arm uses: a working
entry order still resting keeps the skip, nothing resting hands the position to
the sweep to re-protect. No number was chosen and none was changed. The residual
risk is taken deliberately and on the conservative side: a live session slower
than every window ever measured may have its add blocked by the stop the sweep
places, and the add's own failure path restores from the write-ahead row — an
add refused with the position protected beats a position left naked.

**The amend question, answered for the last time.** A quantity amend on the
resting stop cannot replace the cancel, for two independent reasons: the resting
protective SELL collides with the working BUY whatever its quantity says, and
this broker refuses a quantity amend on a fractional order (42210000), which
scale-in adds routinely are. A test now pins that the path never reaches for
`replace_order_by_id` at all, so the refusal cannot be hit and the resting stop
is never left in an unknown state.

**Still open.** The second DONE WHEN (explaining the historical write-ahead-log
row-id gap, so the 14-pair count is known complete rather than a floor) is
unaddressed: the new event makes FUTURE pairs complete by construction but says
nothing about the rows already filed. The third (answering "is any position
naked right now, and for how long" without a one-off query) is also unmet — the
new event is still something you have to go and read, and the coverage watchdog
deliberately skips symbols mid-scale-in, which is exactly this window.

**What was deliberately not done.** No fix, no alert, no change to `scale_in.py` — including the docstring's "~15 s", which the measurement contradicts but which is a code change and not this pass's mandate.

## item 193 — RETIRED 2026-10-01, the write-ahead row-id gap is explained: the id sequence is shared with the ordinary protective-sell restore path, every one of the 15 production cancels carries its own row id, and all 15 pair with a later rearm, so the pair count is complete and not a floor

Measurement only. No production code was written or changed for it; both events already exist.

**The two event names, as they actually appear in the code.** The cancel is filed in `src/pipeline_stages.py` as stage `scale_in`, outcome `protective_sell_cancelled`, reason `cancel_confirmed_via_trade_updates` — the reason string is stale wording kept deliberately, since the `trade_updates` socket has been off since 2026-09-17 and the confirm is now a bounded REST wait. The rearm has NO scale-in-specific event: it is the generic stage `protection`, outcome `placed` or `not_placed`, reason `protective_stop_result`, fired for every entry protection whether or not a scale-in preceded it. Pairing therefore has to be done on `run_id` plus `symbol` plus event ordering, which is why a cancel whose run placed no protection at all would show up as unpaired.

**Method.** Both events land in `specialist_evidence` with `kind='pipeline_event'` (via `_record_pipeline_event` -> `_persist_evidence` -> `Database.insert_specialist_evidence`); there is no `pipeline_events` table. The live file was copied out with its `-wal` and `-shm` sidecars and the counts agreed with and without them. Exposure per event is `abs(held_qty_before)` from the cancel event's own payload multiplied by the add's fill price from the `trades` row for the same run and symbol. Volatility is the standard deviation of the last 20 daily close-to-close log returns strictly before the event date, from daily bars for that name, scaled to the window by `sigma_daily * sqrt(seconds / 23400)`. No volatility number was assumed or carried over from anywhere.

**2026-09-30 — the window is now self-measuring, and amending cannot remove it.**
Each scale-in that cancels a protective stop emits one `scale_in` /
`unprotected_window_closed` event (or `unprotected_window_still_open` when the
rearm did not land) at the moment the rearm attempt returns. Both ends are
`time.monotonic()` readings taken inside the run — the broker's cancel
acknowledgement and the broker's rearm acknowledgement — so the figure never
reflects a row's write time, which was the first DONE WHEN. The event carries
`window_seconds`, the WHOLE `held_qty_before` the cancel exposed (abs()ed, so a
short reads positive), `exposed_notional` (None, never a fabricated 0, when the
reference price is unknowable) and the same `wal_row_id` as the cancel event, so
an unpaired cancel is a visibly missing partner rather than something inferred
from row-id arithmetic. Both new outcome words joined `UNDECIDED_OUTCOMES`:
they are mid-add bookkeeping and rule on nothing.

**2026-09-30 — the skip stays, the silence does not.** The coverage sweep
skips any symbol holding a live scale-in write-ahead row, and that skip is
correct: placing a stop there re-creates the opposite-side block the cancel
just cleared. It also meant the one moment the desk is naked was the one
moment the report said nothing, because a skipped symbol simply vanished from
the sweep. It now appears by name — held quantity, short or long, the row's
`created_at`, and roughly how long protection has been down — in the sweep's
run record, in its single log line, and in `CoverageStatus.unguarded`. It is
kept OUT of `gaps`, because a gap is something the sweep tries to repair and
this one must never be repaired. The duration is the write-ahead row's WRITE
time, not the broker's cancel acknowledgement, so it is labelled approximate
everywhere; the exact figure remains the `unprotected_window_closed` event the
session files at rearm. The overdue test is not a chosen number: the bound is
the LONGEST window the desk has actually measured, read back out of its own
closed-window events, and with no measured history there is no bound and
nothing is called overdue. Over the bound, the owner is paged once per symbol
per trading day on its own footing — never folded into the coverage-gap alert,
which would tell him the desk failed to place a stop it in fact cancelled
deliberately. No broker order is placed by any of this.

**Amending does not close this window.** The desk measured on the rehearsal
account that Alpaca amends a resting stop's price in place, and `broker.py`
grew an amend path. It does not apply here. The cancel exists because a resting
protective SELL holds the shares and collides with the BUY add; a price amend
leaves that SELL open, so the collision — and the reason for the cancel — is
unchanged. The quantity amend that WOULD cover an enlarged position is refused
by this broker on a fractional order (42210000), and scale-in adds are routinely
fractional. The window is a property of the broker's order model, not of the
desk's sequencing, so the remaining work is detection and bounding, not removal.

**2026-09-30 — the lock-held skip is now BOUNDED, which is the only part of
this that was ever removable.** The coverage sweep's skip had two arms. The
crash arm was already safe: with no session lock it skips a symbol only while a
working entry order still rests, so a dead session's naked position is repaired.
The session-lock arm was not: while the wrapper's lock directory existed, every
symbol holding a scale-in write-ahead row was skipped for as long as that row
survived, so a session that cancelled the protective stop and then HUNG without
releasing the lock left the whole held position naked with the one watchdog that
could re-protect it deliberately looking away, with no end. The lock arm now
defers to the desk's own measurement: within the longest window it has ever
closed and recorded — and always when it has measured nothing at all, or cannot
read a row's write time — behaviour is byte-for-byte what it was, because that
is a normal live window. Past that bound the lock stops being reason enough, and
the symbol falls back to the same collision test the crash arm uses: a working
entry order still resting keeps the skip, nothing resting hands the position to
the sweep to re-protect. No number was chosen and none was changed. The residual
risk is taken deliberately and on the conservative side: a live session slower
than every window ever measured may have its add blocked by the stop the sweep
places, and the add's own failure path restores from the write-ahead row — an
add refused with the position protected beats a position left naked.

**The amend question, answered for the last time.** A quantity amend on the
resting stop cannot replace the cancel, for two independent reasons: the resting
protective SELL collides with the working BUY whatever its quantity says, and
this broker refuses a quantity amend on a fractional order (42210000), which
scale-in adds routinely are. A test now pins that the path never reaches for
`replace_order_by_id` at all, so the refusal cannot be hit and the resting stop
is never left in an unknown state.

**2026-10-01 — the row-id gap is EXPLAINED, and the pair count is COMPLETE.**
Measured read-only against the production database
(`/home/qamc/quant-agent/data/quant_agent.db`, snapshot taken 2026-10-01; the
repo-local DB is empty): 15 `scale_in|protective_sell_cancelled` events exist
(2026-09-17..2026-09-30, one more than the 14 of the first pass), ALL 15 pair
with a later same-run same-symbol `protection|placed`, and ZERO
`scale_in|skipped` events of any reason have ever been filed, so no preparation
has yet aborted between the write-ahead insert and the confirmed cancel. Each
cancel event now carries the row id it allocated: 5, 6, 7, 9, 10, 11, 12, 13,
14, 15, 16, 18, 19, 20, 23, against an AUTOINCREMENT sequence standing at 23.
The eight ids scale-in does not hold — 1, 2, 3, 4, 8, 17, 21, 22 — belong to the
OTHER writer of the same table, the ordinary protective-sell restore path in
`src/pipeline.py`. The sequence is shared, so the highest row id was never a
count of scale-ins and the apparent "20 ids vs 14 events" shortfall was an
artefact of reading one writer's census off two writers' counter. The census
that is correct filters on the sentinel `sell_order_id`, and
`tests/test_scale_in_wal_row_id_census.py` pins both properties so the argument
stays mechanical. No production code was changed by this pass and no broker
order was placed.

**The amend merged to main does not reach this path — verified in code, not
assumed.** `_amend_resting_stop_price` in `src/execution/broker.py` has exactly
one caller, the trailing-stop re-price, and it amends a stop's PRICE. A price
amend leaves the protective SELL resting, which is the thing that collides with
the BUY add, so it cannot replace the cancel; the quantity amend that would
cover an enlarged position is refused by this broker on a fractional order
(42210000). `tests/test_scale_in.py` already pins that the scale-in path never
reaches for `replace_order_by_id`. The window is therefore still real, and this
item closes on measurement and detection, not on removal.

**Nothing remains open** (the paragraph below is kept as the record of what was
open on 2026-09-30; the second DONE WHEN was settled on 2026-10-01 above, and
the third was met on 2026-09-30 by the coverage sweep's named skips).

**Was open on 2026-09-30.** The second DONE WHEN (explaining the historical write-ahead-log
row-id gap, so the 14-pair count is known complete rather than a floor) is
unaddressed: the new event makes FUTURE pairs complete by construction but says
nothing about the rows already filed. The third (answering "is any position
naked right now, and for how long" without a one-off query) is also unmet — the
new event is still something you have to go and read, and the coverage watchdog
deliberately skips symbols mid-scale-in, which is exactly this window.

**What was deliberately not done.** No fix, no alert, no change to `scale_in.py` — including the docstring's "~15 s", which the measurement contradicts but which is a code change and not this pass's mandate.

## item 193 — RETIRED 2026-10-01, the id-ceiling gap is explained: the WAL id sequence is SHARED with the protected-sell exit path, all 15 production cancels carry their own wal_row_id and all pair, and every id handed out is now recorded durably at the single insert choke point (detail moved from the board 2026-09-30)

`src/execution/scale_in.py` states the property itself: an add to a held name cancels the resting protective sell, confirms the cancel, submits the BUY, then rearms protection covering the full position. Nothing is protected in between, and the window's length does not depend on the size of the add, so a small nudge exposes the entire holding. Item 183 removed the minimum-trade-size floor that used to turn tiny adjustments into do-nothing holds, so small adds can now reach the broker and open this window. MEASURED against the live production database (`/home/qamc/quant-agent/data/quant_agent.db`, the only non-empty one; the two other `.db` files on that box are 0 bytes): 14 `scale_in|protective_sell_cancelled` events exist over 2026-09-17..2026-09-24, and ALL 14 pair with a later same-run, same-symbol `protection|placed` event — ZERO unpaired cancels, corroborated independently by `pending_protection_restores` holding zero rows, so no position in the record was left naked and never re-armed. Window length median 1 s, worst 4 s, three pairs at 0 s (the event timestamps are whole seconds, so 0 s means under the resolution floor, not instantaneous). Exposure while naked: median $1,209, worst $2,733, $17,855 summed across all 14 — every one of them the FULL holding, not the add. Expected adverse move over a window of that length, using each name's own 20-session close-to-close log-return standard deviation from daily bars and square-root-of-time scaling across a 6.5-hour session: median $0.24, worst $0.73, $3.67 summed over all 14 — sub-dollar at the sizes this book has traded. The property is therefore DOCUMENTED AND REAL but NOT CURRENTLY COSTLY, and the module's own docstring estimate of a "~15 s" window OVERSTATES the measured record by roughly four times. WHAT THIS CANNOT ESTABLISH, and why no remedy is proposed here: the timestamps are DB-write times at second resolution, not broker cancel-ack and rearm-ack times, so they bound the window rather than measure it; 14 pairs over 8 calendar days is too thin to call a tail, and the worst case scales with position size and with any broker slowness this sample never saw; the write-ahead-log row ids reached 20 while only 14 cancel events exist, so up to six preparations may have cancelled without filing an event, which would make even the pair COUNT a floor; the volatility figure is a diffusion estimate over a few seconds, not a measurement of what those seconds actually did, and it prices an ordinary move rather than a gap or a halt, which is the case a protective stop exists for. Someone else decides the remedy.


