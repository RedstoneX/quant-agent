## item 193

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

**Still open.** The second DONE WHEN (explaining the historical write-ahead-log
row-id gap, so the 14-pair count is known complete rather than a floor) is
unaddressed: the new event makes FUTURE pairs complete by construction but says
nothing about the rows already filed. The third (answering "is any position
naked right now, and for how long" without a one-off query) is also unmet — the
new event is still something you have to go and read, and the coverage watchdog
deliberately skips symbols mid-scale-in, which is exactly this window.

**What was deliberately not done.** No fix, no alert, no change to `scale_in.py` — including the docstring's "~15 s", which the measurement contradicts but which is a code change and not this pass's mandate.

## item 193 — detail moved from the board 2026-09-30

`src/execution/scale_in.py` states the property itself: an add to a held name cancels the resting protective sell, confirms the cancel, submits the BUY, then rearms protection covering the full position. Nothing is protected in between, and the window's length does not depend on the size of the add, so a small nudge exposes the entire holding. Item 183 removed the minimum-trade-size floor that used to turn tiny adjustments into do-nothing holds, so small adds can now reach the broker and open this window. MEASURED against the live production database (`/home/qamc/quant-agent/data/quant_agent.db`, the only non-empty one; the two other `.db` files on that box are 0 bytes): 14 `scale_in|protective_sell_cancelled` events exist over 2026-09-17..2026-09-24, and ALL 14 pair with a later same-run, same-symbol `protection|placed` event — ZERO unpaired cancels, corroborated independently by `pending_protection_restores` holding zero rows, so no position in the record was left naked and never re-armed. Window length median 1 s, worst 4 s, three pairs at 0 s (the event timestamps are whole seconds, so 0 s means under the resolution floor, not instantaneous). Exposure while naked: median $1,209, worst $2,733, $17,855 summed across all 14 — every one of them the FULL holding, not the add. Expected adverse move over a window of that length, using each name's own 20-session close-to-close log-return standard deviation from daily bars and square-root-of-time scaling across a 6.5-hour session: median $0.24, worst $0.73, $3.67 summed over all 14 — sub-dollar at the sizes this book has traded. The property is therefore DOCUMENTED AND REAL but NOT CURRENTLY COSTLY, and the module's own docstring estimate of a "~15 s" window OVERSTATES the measured record by roughly four times. WHAT THIS CANNOT ESTABLISH, and why no remedy is proposed here: the timestamps are DB-write times at second resolution, not broker cancel-ack and rearm-ack times, so they bound the window rather than measure it; 14 pairs over 8 calendar days is too thin to call a tail, and the worst case scales with position size and with any broker slowness this sample never saw; the write-ahead-log row ids reached 20 while only 14 cancel events exist, so up to six preparations may have cancelled without filing an event, which would make even the pair COUNT a floor; the volatility figure is a diffusion estimate over a few seconds, not a measurement of what those seconds actually did, and it prices an ordinary move rather than a gap or a halt, which is the case a protective stop exists for. Someone else decides the remedy.


