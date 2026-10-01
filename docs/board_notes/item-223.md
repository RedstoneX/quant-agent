## item 223

Filed 2026-10-01 out of item 217's truth pass, and not fixed there. The portfolio-manager prompt told the seat that a target below `min_position_risk_pct` would be denied by the constructor anyway. It is not. `allocate_risk_budget` (`src/risk/budget.py` 304-310) grants a request IN FULL whenever `requested <= allowed`, however small it is; the `floor_pct` denial at 332-343 is reached only by a grant the budget had already CUT below the floor. The allocator is also skipped entirely when existing book risk is unreadable (`src/portfolio_constructor.py` 1972-1983), and the branch at 2034-2035 then sets `granted = requested`. So a sub-floor target that fits the remaining headroom is sized and shipped: a token position paying full commission and full attention for an immaterial payoff, which is exactly what the floor exists to prevent.

The prompt sentence is corrected under item 217, so the floor now rests on the seat alone and the seat is told so. What is NOT decided: whether the desk wants a deterministic sub-floor refusal in the constructor, or is content for the floor to be guidance the seat applies. That is a ruling, not a wording fix, which is why no behaviour was changed.


