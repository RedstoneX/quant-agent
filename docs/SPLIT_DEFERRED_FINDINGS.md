# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

## The midnight clock bug (HALF DONE -- cost-circuit half still open)

Between 00:00 and roughly 00:16 Eastern, tests compare an exchange trading day
against the runner's local day and fail. Seven in the holding-discipline
intraday file, thirteen more in the cost circuit. They pass again at 00:20 ET.
Verified twice on 2026-10-02. This is the third or fourth instance of the same
class. It costs a full test round every time it fires and it reds every open
change at once, which is why it goes first.

DONE 2026-10-02: the holding-discipline half. Root cause was a module-level
`str(et_today())` stamped when the file is COLLECTED, compared against an
`et_today()` read when the test RUNS -- a suite that crosses ET midnight
between the two compares two different exchange days. Reproduced on demand by
shifting the clock (collect 23:58 ET, run 00:05 ET): 6 failed before the fix,
26 passed after, at the same simulated instant. The stamp is now read at run
time. A mechanical guard, `tests/test_no_local_day_as_exchange_day.py`, now
fails on `date.today()`, a naive `datetime.now()`, a UTC calendar day used as
a day, and an import-time clock stamp in a test, with a shrink-only baseline
of the offenders that already existed.

STILL OPEN: the cost-circuit half. Under the same clock shift (00:02, 00:05 and
00:10 ET, Python and SQLite moved together) none of the age-latch tests failed,
so the thirteen are UNPROVEN rather than diagnosed -- the simulator pins SQLite
at connect time, which may be hiding it. Next step: reproduce against the real
runner clock, or recover the original failing test names, before changing code.
The guard's baseline lists the remaining offenders; they are candidates for the
same fix, not approvals.

## The evidence gate's per-name record has never been written -- FIXED

In the name-coverage loop the recording call passed a symbol argument twice --
once by name and once inside the unpacked details -- so every call raised a
type error, which a broad catch turned into a log line. A second collision sat
behind it: the stage was also passed twice, so removing only the symbol still
wrote nothing. Both were reproduced before the fix: the old code logged
"got multiple values for argument 'symbol'", and a run through the morning
session left zero per-name rows in the store. Fix: the details now carry
neither key. Proof: a new test runs the gate and asserts a real per-name row
for a candidate lands in the store with the right stage and outcome. The broad
catch stays, deliberately -- this runs on the trading path and a forensic
record may not stop it -- but it now logs the full traceback at error level,
and the store-level test is what keeps this class from hiding again.

## Owner alerts are sent and the result thrown away

Nearly every caller discards the return value of the owner-alert send, so a
failed delivery is indistinguishable from a successful one. Fix: make the
callers honour the result, and prove a failed send is visible somewhere.

## A broker read error reads as "no stop to adjust" -- FIXED

The current-stop-price read used to return nothing on ANY error, and the
protection path treated nothing as "there is no stop here" and skipped. Now
there are three answers (found, none, unreadable) in `src/execution/stop_read.py`.
The broker raises when it cannot tell, `read_stop` turns that into an
`unreadable` answer whose price cannot be read by accident, and every
unreadable answer is written to the evidence table and sent to the owner once
per symbol per day, worded as "could not read the stop", never "no stop". All
seven callers use it (ex-dividend shift, deterministic trail, midday
minimum-ratchet floor, two prompt-facts reads, evening stop proximity, stop
level reconcile). Proven: with the read raising, the ex-dividend path recorded
nothing and alerted nobody before the fix (red) and records and alerts after;
a genuine "no stop" still skips quietly; the ambiguous both-sides case is now
"unreadable" too. Open: the evening proximity and reconcile callers have no
database handle, so they alert but write no row.

## The cost circuit is eleven mixins, not eleven modules

It sits under the ceiling, but eleven of its nineteen pieces are mixin groups,
which cannot be built or exercised on their own. Smaller files, not
boundaries. Fix: convert them the way the sessions, exits, protection, broker
and storage packages were done, and add witness tests.

## Real boundaries still owed

The position builder, the portfolio-manager seat and the prompt-facts review
chunk are under the ceiling but are not separable pieces. Same treatment.

## `update_open_take_profit` refuses through an undefined name

`update_open_take_profit` in the storage layer reaches for a bare `_log` that
is not defined in its module, so the refusal branch raises `NameError` instead
of recording the refusal. Pre-existing on `main` before the database rebuild;
the body moved verbatim into `src/storage/trades/ledger.py`, so the defect
moved with it unchanged. Found 2026-10-02 during database instalment 3. Fix
after the structure is sound: give the module its logger, then prove the
refusal path records rather than raises.
