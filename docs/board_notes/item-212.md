## item 212

Filed 2026-09-30 alongside the revert of PR #857.

What is true in the code: for a Type A (range) entry the structural and
chandelier trail does not run at all until price exceeds the recorded
take-profit target. The +1R breakeven lock and the +2R second ratchet are
separate, ratified, and unaffected.

Why the target gate is a defect: the target is an unsourced number, and the
desk's doctrine bars an unsourced number from governing an exit. Between
entry and the target the position has only its original entry stop, so
nothing follows price up through the part of the move the trade actually
spends most of its life in.

Why PR #857 was NOT the fix, and was reverted: it moved the gate from the
target to +2R. Measured on 33 real production BUY trades, the target's
reward-to-risk is median 1.33 and at most 1.72 — never as high as 2.0. The
target is therefore reachable in practice and +2R never has been, so the
change made range positions LESS trailed on live data, not more. The
backtest showed no difference only because its simulated targets sat nearer
still.

Also verified: the target cannot close or cap a position. No take-profit
order is ever sent to the broker and a target rationale cannot authorise a
sale, so gating this trail is the target's only live behaviour.

The owner's ratified answer is exit-on-alignment: sell when structure, ATR
and an SMA cross agree the trend is over, never on a single made-up level.
The alignment exit on open PR #853 is the candidate replacement for this
gate. Do not build a replacement under this item, and do not re-derive,
widen or replace any multiple.
