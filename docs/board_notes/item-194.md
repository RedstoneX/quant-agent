## item 194

**Filed 2026-09-30.** When the desk buys something it works out, from the chart, the nearest price level the stock has to get through on the way up, and that becomes the profit target it quotes you. The number is then frozen for the life of the position.

Until now the desk only revisited that number when the level it was measured against **disappeared** — the stock jumped clean over it and the ceiling was gone. It never revisited it when the opposite happened: the stock spent a few weeks building a **new** ceiling somewhere between where the desk bought and the number it was quoting. In that case the desk carries on quoting a target with a wall in front of it, which is exactly the thing the target is supposed to be. That is now fixed — a new wall counts as a reason to work the number out again, the same way a broken wall does. The number is still always **worked out from the chart**, never typed in, and still measured from the original buy price so it cannot drift upwards just because the stock went up.

What is left open is the **way in**. The recalculation only runs on a stock one of the desk's analysts has specifically raised a hand about. A stock that quietly grows a wall while nobody mentions it gets reported every morning and never recalculated. Two positions are in exactly that state right now: Apple and Nokia. Whether the morning report should be allowed to trigger the recalculation by itself is the open question — it would mean an automatic change to a live position's record, which is not something to switch on without a decision.

**No owner decision needed on the fix itself** — it is the same measurement the desk already does, run in one more circumstance. The open question above may need one, because it changes live position records without a human in the loop.

## item 194 — detail moved from the board 2026-09-30

`src.risk.target_revision` gained `TRIGGER_WALL_IN_FRONT_OF_TARGET`: a structural level still in the way standing between the entry and the stored target is now a structural event that legitimises a re-derivation, the mirror of `TRIGGER_LEVEL_BROKEN`. The residue is the way in, not the trigger. `assess_target_revision` only ever runs on a symbol a seat has raised a `TargetRevisionFlag` for, so a position whose chart grows a wall while no seat happens to mention it is reported daily by `quant-agent-stored-target-check.timer` and never re-derived. Measured on the live book 2026-09-30, that is AAPL (stored $359.93, wall $344.81) and NOK (stored $12.25, wall $11.09), neither of which any seat had flagged. Whether the guard's own finding should itself be a way in — a deterministic, non-LLM path into the same adjudication — is the open question, and it writes to live position records, so it is not self-authorised.


