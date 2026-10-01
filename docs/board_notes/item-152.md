## item 152 — detail moved from the board 2026-09-30

A parse failure means the call was paid for and thrown away with nothing to show for it; measured on the retained logs: 11 on the news seat, 79 on the technical seat [measured 2026-09-18 against `quant_agent.log` and its five rotations]. Re-measured 2026-09-26: 7 of the 11 survive in the retained rotations (2026-08-21..09-02, the rest aged out), 5 of them the single `market_sentiment` field carrying a word outside the three legal ones, 2 genuinely unsalvageable, none the whole-answer non-JSON case.


