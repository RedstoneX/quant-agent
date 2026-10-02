# Findings parked during the rebuild

Bugs noticed while moving code, deliberately NOT fixed inside a split: a
behaviour change hidden in a verbatim move is unreviewable. Each entry says
what is wrong, where, and what would prove a fix. Work them after the
structure is sound, hardest-wearing first.

## The midnight clock bug (work this FIRST)

Between 00:00 and roughly 00:16 Eastern, tests compare an exchange trading day
against the runner's local day and fail. Seven in the holding-discipline
intraday file, thirteen more in the cost circuit. They pass again at 00:20 ET.
Verified twice on 2026-10-02. This is the third or fourth instance of the same
class. It costs a full test round every time it fires and it reds every open
change at once, which is why it goes first. Fix: compare exchange day to
exchange day, and add a guard that fails when a test reads the local date.

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
