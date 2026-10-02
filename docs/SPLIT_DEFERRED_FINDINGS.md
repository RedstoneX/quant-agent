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

## The evidence gate's per-name record has never been written

In the name-coverage loop the recording call passes a symbol argument twice --
once by name and once inside the unpacked details -- so every call raises a
type error, which a broad catch turns into a log line. Verified against the
main line; observed firing live in a test-suite log. The gate therefore has no
per-name record at all, and anything closed against that recording was closed
against nothing. Fix: pass the details without the duplicate key, then prove
rows actually appear.

## Owner alerts are sent and the result thrown away

Nearly every caller discards the return value of the owner-alert send, so a
failed delivery is indistinguishable from a successful one. Fix: make the
callers honour the result, and prove a failed send is visible somewhere.

## A broker read error reads as "no stop to adjust"

The current-stop-price read returns nothing on ANY error, and the protection
path treats nothing as "there is no stop here" and skips. A transient read
failure therefore silently skips a stop adjustment on a live position. Fix:
separate "no stop" from "could not tell", and make the second one loud.

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
