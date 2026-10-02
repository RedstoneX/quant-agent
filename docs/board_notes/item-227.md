## item 227 — a seat read that cannot say when it was taken

**The hole, in one paragraph.** `src/evidence_gate.py` has classified each
research seat as fresh / carried / absent since 2026-09-18, and that
classification was already persisted inside `session_reports.payload_json`
and `intra_check_reports.payload_json`. What it never carried was
provenance: which run produced it and at what time. A later reader holding
one of those rows could only INFER whether the reading was taken in the run
it was sitting beside, and for a carried seat could not say how old the
answer was at all.

**Why it became the blocker on 2026-10-01.** The owner ruled that any
holding failing the desk's own fresh-entry bar must be sold, and that the
bar is re-tested several times a day — "it has to earn its right to be
there". That makes "was this seat read in THIS run?" a fact a SELL rests
on. The difference between cutting losses fast and selling on stale
information is exactly this recording.

**What the code actually does per session mode.** Established from the code,
not from the failure-log sample. The gate classifies whatever `data_status`
the run populated, so the mapping is a property of which seats the run
fills:

- The morning run fills every analyst seat (`tech`, `news`, `macro`,
  `earnings`, `smart_money`) with a read-this-tick status.
- The half-hourly `intra_check` re-reads the technical seat; the remaining
  seats arrive through the `CarryForward` helper in `src/pipeline.py`, which
  stamps `not_run_intraday` or `chose_not_to_refetch`, both of which the
  gate already buckets as CARRIED and never as fresh. An expired news seat
  can be re-read by the heal path, in which case it reports fresh honestly.
- The midday run re-reads news and runs the position review.
- A seat whose status word the gate does not recognise is reported as
  `unknown` and is never counted as read.

**What shipped.** `EvidenceFreshness.stamped()` attaches the run id, the
session mode and a timestamp without changing any seat's bucket;
`seat_stamps()` turns that into a per-seat record; `seat_read_state()` is
the single predicate a caller uses per name and per seat. The storage layer
gained `last_fresh_seat_reads()`, which reads the stamps back out of the two
report tables it already writes — no new table and no second mechanism — so
a carried seat can report a real age instead of guessing one.

**What deliberately did NOT ship, and must not be added here.** No freshness
threshold, no expiry window, no minimum fresh-seat count, and no decision
gated on any of this. `age_seconds` is reported; nothing says when an age is
too old. That number is the owner's. `tests/test_evidence_gate.py`
mechanically forbids any numeric literal in the gate module and that guard
is still green, which is why the zero age of a just-read seat is computed
rather than written.

**A finding, not a fix.** The consumer that will eventually apply the
fresh-entry bar WILL need to know how old is too old, and nothing in the
desk's own data answers that yet. It is an owner appetite question and it is
left open on purpose.

**Not claimed.** No production session has yet been observed emitting these
stamps; the round trip is proven against a real database file in
`tests/test_seat_read_freshness_stamp.py`, not against production.

## Rehearsal assessment, 2026-10-02

Evidence kind: OFFLINE REHEARSAL runs of 2026-10-02 against a snapshot of the production database. These are NOT production sessions and no box was ticked on them.
Observed runs (ops/rehearsal/run.py, replay pinned automatically, sudo-user snapshot, production file byte-identical after each):
- morning: VERDICT FAIL and "REHEARSAL VOID -- HermeticBreach": the harness blocked outbound connections to the FRED host because no FRED or news feed is recorded on this box (board item 202); 0/15 macro series and 0/20 news feeds returned data; the recording holds ONE portfolio_manager answer and the session asked twice, so every route raised "all 1 recorded response(s) were already replayed" and the session raised in the decision stage.
- midday: VERDICT PASS but "REHEARSAL VOID -- HermeticBreach" (outbound attempts to the FRED host and the Yahoo client blocked).
- intra_check: first run VOID (101 inputs absent from the recording); re-run with --allow-degraded completed, VERDICT PASS, not void, 0 trades, 8.4s, $0.00.
Row read back from the rehearsal intra_check report (sandbox database): run_id rehearsal-intra_check-20261002, evidence_freshness = None. The tick found no candidates, so no seat was read and no stamp was written; the two preceding real intra_check rows (2026-10-01) also carry none.
Last box (production session with a carried seat reporting a real age and a refreshed seat reporting this run's id): NEEDS-REAL-SESSION. The only session type that writes the stamp is one that actually reads seats; morning and midday are void offline until FRED and news feeds are recorded (item 202), and even a clean replay would stamp a rehearsal run id over replayed answers, which is not an observation of the desk. Box left open.
