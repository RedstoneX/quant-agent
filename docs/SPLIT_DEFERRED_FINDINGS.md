# Findings parked during the split
Written down under the split mandate (scope: only the split and the test rebuild).
Come back to these after both oversized files are done.

- **Alert failures are ignored.** `send_owner_alert` returns whether the alert
  actually went out; most call sites in `src/` discard it, so the desk proceeds
  believing the owner was told when the owner was not. Needs a durable record of
  the failure at each ignoring site, not a log line. Parked 2026-10-01: out of
  scope for the split.
- **`broker.get_current_stop_price` returns None on any error.** `pipeline_protection`
  reads that as "no stop to adjust" and skips, so a failure to ASK is
  indistinguishable from nothing being THERE. Verified against main. Parked
  2026-10-01: out of scope for the split, and both files are touched by open PRs.
- **The evidence gate's per-name record has never been written.** In
  `src/pipeline.py` the name-coverage loop calls the local `_record(symbol,
  outcome, reason, **details)` with the name positionally AND a `symbol` key
  inside `**record`, so every call raises `TypeError: got multiple values for
  argument 'symbol'`. A broad `except Exception` turns it into a log line, so
  the gate reports success while recording nothing. VERIFIED by me against
  origin/main, 2026-10-01. Found only because the tests stopped mocking the
  constructor. Parked: out of scope for the split.
- **The cost circuit is under the ceiling but not yet properly separated.** The
  4,300-line file became 19 modules, but eleven of them are `_Breaker*Mixin`
  method groups that still require the whole breaker class to exist before any
  of them can run. That is the same cosmetic-split failure the pipeline had:
  smaller files, no real boundaries. The size win is real and the move was
  proven verbatim, so it stands; the boundary work is a separate piece. Written
  down 2026-10-02 rather than claimed as done.
