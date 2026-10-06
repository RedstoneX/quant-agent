## item 233

**Owner direction, 2026-10-06.** This separate capture-and-replay system is no longer a condition for turning the Paper desk into its beta test. Stop spending time and money expanding it. The earlier work is parked, not presented as a completed real-world proof. Revisit it only if a specific failure needs reproduction and the owner asks. The desk remains off for the separate structural and safety review, not because this test is unfinished.

**Filed and corrected 2026-10-05.** This replaces the frozen second-machine rehearsal without pretending that made-up evidence is real evidence. The owner rejected the earlier proposal to close this with hand-written market, account and broker answers. Those can remain useful small regression checks, but they cannot prove that the actual trading path can be replayed faithfully.

**What replaces it.** First, make a deliberately bounded morning capture and a deliberately bounded intraday capture using only the isolated secondary Alpaca Paper account. Those live captures must travel through the same provider and broker boundaries the desk normally uses. They are source material, not hermetic tests. Before anything is committed, a safety check must reject keys, stable account identity, anything from the primary account, and production decision material. The deliberately test-only values needed to repeat what happened must remain intact.

Second, replay those captures with every network route sealed. That offline replay is the hermetic proof. It must take the real morning and intraday paths, reach the Portfolio Manager, protection and exit decisions that the captured session reached, and stop if an input is absent. A parallel imitation of those paths, or an answer invented after the capture, does not count.

**Worked example.** Suppose the morning capture shows two candidates, one refused for a recorded reason and one passed to the Portfolio Manager. The offline replay must produce those same two candidates and that same refusal and decision while making no network connection. If one recorded market or broker answer is removed, the replay must fail at that missing answer; it may not substitute a plausible value and continue.

**Safety boundary.** The primary account and the production trading run are not used. A live call is permitted only for the bounded capture against the isolated secondary Paper account. The resulting proof is the later offline replay, not the live capture itself.
