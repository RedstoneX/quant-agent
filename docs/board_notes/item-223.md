## item 223 — RETIRED 2026-10-01, ruled on the risk route: no deterministic sub-floor refusal, recording built instead

Filed 2026-10-01 out of item 217's truth pass, and not fixed there. The portfolio-manager prompt told the seat that a target below `min_position_risk_pct` would be denied by the constructor anyway. It is not. `allocate_risk_budget` (`src/risk/budget.py` 304-310) grants a request IN FULL whenever `requested <= allowed`, however small it is; the `floor_pct` denial at 332-343 is reached only by a grant the budget had already CUT below the floor. The allocator is also skipped entirely when existing book risk is unreadable (`src/portfolio_constructor.py` 1972-1983), and the branch at 2034-2035 then sets `granted = requested`. So a sub-floor target that fits the remaining headroom is sized and shipped: a token position paying full commission and full attention for an immaterial payoff, which is exactly what the floor exists to prevent.

The prompt sentence is corrected under item 217, so the floor now rests on the seat alone and the seat is told so. What is NOT decided: whether the desk wants a deterministic sub-floor refusal in the constructor, or is content for the floor to be guidance the seat applies. That is a ruling, not a wording fix, which is why no behaviour was changed.

RULED 2026-10-01 **on the RISK ROUTE, on the adversary's measurement —
NOT by the owner**, which item 223 itself permits ("the owner or the risk
route"). The distinction is load-bearing: an owner ruling carries an
authority nothing else on this desk does, and a reader who believes the
owner settled this will not re-open it when the evidence changes. This one
may be re-opened on new evidence. The ruling: **NO deterministic refusal.
Build the RECORDING instead.** Four reasons, recorded in full so neither the refusal
nor a re-argument of it is tried again.

1. **It would have fired zero times.** Measured over 142 portfolio-manager
   logs from 2026-08-17 to 2026-09-30, 115 targets carried a risk allocation
   and ZERO were positive-but-below-floor. The only four below the floor were
   exactly zero, and a zero already routes to CLOSE rather than to a buy. A
   deterministic refusal would harden a gate on a path nobody has walked.
2. **The floor is ratified appetite, not a derived number.** It is 0.5 and
   the number ledger called it `sourced`, but its stated source is the
   owner's own ratification plus the settings file. This desk's own rule is
   that a ratified point is not a sourced point, and hardening a ratified
   number into a hard refusal is the opposite of what the doctrine wants.
3. **The desk already had this refusal and retired it.** A hard sub-floor
   refusal block existed and was removed on 2026-09-11, precisely because a
   universal cutoff was the wrong shape. The history on this exact question
   is a refusal that was taken out, not one that was kept.
4. **Enforcement is not what is missing; evidence is.** The portfolio-manager
   prompt already instructs the seat not to emit below the floor, and
   measured compliance is 115 of 115. What nothing could answer was whether
   the rule is ever broken.

WHAT SHIPPED. `PortfolioConstructor._plan_risk_targets` now writes one
durable row whenever a target arrives with a POSITIVE risk allocation below
the floor: the symbol, the risk asked for
(`trade_refusals.requested_risk_pct`, a column added for this), the floor in
force (`threshold`) and what the desk then did with it (`stage`, always
`observed_not_refused`). Nothing is refused, nothing is resized, nothing is
reordered — the target falls through to exactly the path it would have taken
had the block not existed, and a test pins that it still ships. The row is
written from executable product code at the call site named above, reached on
every session that plans risk targets. Classified **UNPROVEN**: the write is
reached but has never fired in production, which is the expected and
acceptable answer given reason 1. It is not DEAD — dead would mean no
executable path reaches it.

LEDGER. Both rows the number ledger carries for this one floor
(`src.risk.constants.STARTER_POSITION_RISK_PCT` and
`src.portfolio_constructor.ConstructorConfig.min_risk_pct`) moved from
`sourced` to `arbitrary`, the status this repo uses for owner-ratified
appetite, each with the open question, the cost of leaving it unanswered and
the new recording as its BUILT settlement route. The arbitrary ratchet moved
by +2 with the reason appended. The second row was converted alongside the
first because one number cannot hold two statuses; no value changed.

FINDING, NOT FIXED HERE. That one floor is ledgered twice at all —
`src.risk.constants.STARTER_POSITION_RISK_PCT` and
`src.portfolio_constructor.ConstructorConfig.min_risk_pct`, the same 0.5 —
is the one-definition-per-quantity rule broken, the same shape as the
short-side gap haircut the desk collapsed earlier on 2026-10-01. It should
be collapsed deliberately rather than discovered a third time. The
`config/settings.yaml:751` citation was stale in four places and now reads
685, where the key actually lives.

CORRECTION TO THE RECORD. The adversary reported that no file references the
`trade_refusals` table. That is FALSE: it is written from
`src/portfolio_constructor.py` and `src/storage/db.py`, with tests covering
it. Its zero production rows mean the parity refusal has not fired since it
shipped, which is UNPROVEN, not dead.

DONE WHEN:
  - [x] the owner (or the risk route) rules whether a sub-floor target is
        refused deterministically or left to the seat — RULED on the RISK
        ROUTE (not by the owner): left to the seat, with a recording
  - [x] whichever way it is ruled, the behaviour and the portfolio-manager
        prompt sentence say the same thing — the prompt now states plainly
        that the floor is an instruction to the seat, that nothing
        downstream refuses, resizes or reroutes a sub-floor target, and that
        a breach is recorded but never refused
