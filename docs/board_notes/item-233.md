## item 233

**Filed 2026-10-05.** Spun out of item 202 when it closed. Item 202's ninth DONE WHEN box ("a real rehearsal against the production snapshot returns a verdict it is entitled to give") depended on the rehearsal rig, and the owner ruled on 2026-10-04 that the rig is frozen: too expensive to keep a second machine. The box could therefore never be ticked and is deferred here rather than ticked or silently dropped (commit trailer `Done-criteria-deferred: 202/9 -> item 233`).

**What replaces it.** Hand-written hermetic end-to-end tests. Everything the rehearsal needed is already built under item 202: every outbound route is recorded and replayed, a missing recording stops the run, the conftest network guard journals and fails any test that reaches off-box, and the suite is closed at the socket with no allow-list. What is missing is a test that drives a WHOLE session (morning research, the decision stage, the Portfolio Manager, protection; then the intraday re-read and exit engine) from those recordings and asserts what it decided, not just that it finished. The last settling run (item 202 note, update 6) completed end to end and placed no order; it was never turned into a test.

**Not in scope.** No new recording infrastructure, no second machine, no live provider calls, no production snapshot. If a recorded input is missing, record it by the existing pattern and prove the gap closed with a red test first.

**Evidence kind.** Test-only; nothing here waits on a live run.
