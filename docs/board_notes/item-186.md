## item 186 — detail moved from the board 2026-09-30

UPDATE 2026-09-30 (short-side haircut) — TWO DERIVATIONS ATTEMPTED, BOTH
WITHDRAWN, NO SIZING CHANGE SHIPPED. The desk sizes shorts exactly as it did
before this pass. `RiskConfig.short_gap_risk_multiple` (1.5) and the
constructor's mirror of it are both untouched and both still
`status: arbitrary`.

Why each attempt died, recorded so nobody spends a third one on either:
- WORST HISTORICAL GAP over the lookback the desk already fetches. The
  lookback is a ~5-year window chosen for CHART STRUCTURE, not for a gap
  distribution, and a maximum over a fixed window can only grow until the bar
  ages out. One old gap would govern every short's size for years, and a
  regime change could not update it. That is a number fitted to past
  outcomes on a sizing path, which doctrine bars.
- STOP DISTANCE PLUS ONE ATR. Dead on algebra, not on data. The entry stop is
  ITSELF placed at 2.5 ATR, so the multiple is (2.5 + 1) / 2.5 = exactly 1.40
  for every name — the per-name ATR cancels out of the ratio. It is a flat
  haircut wearing a per-name costume, and it is LOWER than the 1.5 it claimed
  to replace, so it would have quietly opened every short LARGER.

ALSO ESTABLISHED, AND THE REASON A LEDGER LINE HAD TO BE PULLED. Execution
sizes with min(qty_by_alloc, qty_by_risk), and the risk-budget leg in
`src/pipeline_stages.py` multiplies by `RiskConfig.short_gap_risk_multiple`.
A constructor-only change therefore does NOT retire that number: whenever the
risk leg binds, the execution-side read is the one that sizes the live short.
The withdrawn work carried a ledger line saying the number "NO LONGER SIZES
ANY SHORT". That was untrue and is removed rather than softened. The
construction / execution split is a latent defect in its own right and is
filed as board item 216.

WHAT SHIPPED INSTEAD — THE RECORDING. The reason this number cannot be read
off the instrument is not that the reading is hard; it is that the desk has
never kept the evidence. Bars are fetched live each session and discarded and
there is no OHLCV table, so there has never been a record of what a short
actually suffers overnight. That is now recorded, on the trade row, beside
the facts already pinned at entry:
- `trades.max_adverse_overnight_gap` — the worst adverse overnight gap
  (session open minus prior session's close, in price units, positive =
  against the short) observed on any session the short was held. Stored
  SIGNED and unfiltered, so a short whose every gap ran in its favour records
  a negative worst, which is a real and different fact from "never observed".
- `trades.overnight_gap_sessions` — how many sessions a gap was actually
  observed on, so absence of evidence stays distinguishable from evidence of
  absence.
- `trades.last_overnight_gap_date` — idempotence by date; a second position
  sync in one session cannot count the same gap twice.
These sit on the same opening row as `entry_atr` (the volatility read at
entry) and `initial_stop_loss` (the stop distance), and join to
`realized_pnl` / `exit_reason_category` when the position closes. Written by
`PortfolioManager._record_short_overnight_gaps` off the existing position
sync, shorts only, fail-soft per symbol.

HARD LIMIT ON ITS USE, same as the stop-floor excursion evidence beside it:
RECORDING ONLY. No threshold, no gate, no sizing change; nothing reads it
back into a trading decision. It may NOT be swept for the multiple that would
have been optimal — that is fitting a number to this desk's own history,
which doctrine bars whatever the sample size. What it can eventually support
is a statement about the DISTRIBUTION of adverse short gaps relative to the
stop distance and the volatility read, which is a measurement, not a fit.

The item's completion criterion is changed on the board to match: it closes
on that recording plus enough closed shorts to read, never on another
derivation.


UPDATE 2026-09-30 (second pass, owner ruling on global risk dials). Live-code
inventory of every portfolio- and cluster-level ceiling still standing, each
verified in source this pass, not from the board:

  * `RiskConfig.max_portfolio_risk_pct` = 25 — total capital at risk across the
    book. Owner-ratified 2026-09-25, unsourced. AGGREGATE rationing, not a
    per-name risk read, so the new ruling does not convert it into a defect;
    there is no instrument to read a book-wide ceiling off. Stays, labelled.
  * `RiskConfig.SECTOR_HARD_CEILING_MAX` = 90 (mirrored at the constructor as
    `max_sector_hard_pct`) — terminal sector ceiling. Same shape, same verdict.
  * `RiskConfig.max_cluster_risk_share_pct` = 40 — share of total risk one
    correlation cluster may hold. Same shape, same verdict. What defines a
    cluster is no longer a number (see above); what a cluster may hold still is.
  * `correlation.CLUSTER_CORRELATION_THRESHOLD` = 0.7 — GONE, confirmed absent
    from live code this pass; the module keeps only a comment saying it used to
    be there.
  * `RiskConfig.short_gap_risk_multiple` = 1.5 (mirrored on ConstructorConfig)
    — genuine per-name risk appetite, and therefore a defect under the ruling.
  * `TradingPipeline._clamp_queued_earnings_buys(max_pct)` = 5 — genuine
    per-name risk appetite, and therefore a defect under the ruling.

NEITHER OF THE TWO DEFECTS WAS REPLACED, AND NEITHER WAS ROUTED TO THE OWNER.
Plainly, why:

  * The short haircut's honest per-name form is that stock's own overnight-gap
    magnitude relative to its stop distance. The sizing sites
    (`_build_short`, and the risk-plan loop) receive `analysis.atr_14` and a
    stop price; no bar history reaches them and the database holds no OHLCV
    table, so the gap term cannot be read. Substituting "one ATR of gap" would
    invent the coefficient, which is the thing doctrine bars, so it was not
    done. Unblocked by a stored daily-bar build and nothing else.
  * The queued-earnings clamp's honest per-name form needs that name's expected
    earnings-day move; the desk has no implied-move or historical-reaction
    source, so the same blocker applies. There IS a threshold-free alternative
    that needs no number at all — an unread filing means the fundamental seat
    is not convicted, and standing doctrine already says all five seats must be
    right to enter, so the BUY would be refused rather than capped. That turns
    a size cap into a block on live capital and belongs in front of the
    adversary first, so it is recorded here and not shipped.

Both numbers keep `status: arbitrary` in the ledger with the blocker named and
the withdrawn appetite question marked withdrawn. No value was picked.

UPDATE 2026-09-30: the correlation-cluster cutoff (0.7) is REMOVED rather than
ratified. Cluster membership is read structurally — Mantegna correlation
distance, minimum spanning tree, cut at the tree's own largest edge-length gap
— so there is no level to pick and the routed owner-appetite question on it is
withdrawn. The clustering stays transitive on purpose (a theme transmits by
chaining), and it rations only; the desk still never buys to diversify. The
remaining appetite questions on this item (short-size ratio, overnight
earnings tolerance) are untouched.

Item 90's half two, surfaced for visibility. What caps deployment and crowding is flat and unsourced: the 25% total at-risk portfolio ceiling (`RiskConfig.max_portfolio_risk_pct`), the 90% terminal sector-ceiling bound (`RiskConfig.SECTOR_HARD_CEILING_MAX`, whose definition site says it is "open for the owner to move"), the 40% share of total risk one correlation cluster may hold (`RiskConfig.max_cluster_risk_share_pct`), the 0.7 correlation cutoff that defines what counts as one cluster (`correlation.CLUSTER_CORRELATION_THRESHOLD`), the 1.5x short-side sizing haircut (`RiskConfig.short_gap_risk_multiple`), and the 5% resulting-weight cap on a BUY whose earnings filing is queued but unanalysed (`_clamp_queued_earnings_buys`). All `status: arbitrary`. The already owner-ratified ceilings (per-trade 5%, gross 2.0x, single-name 65% notional, sector soft/hard 75 / 90 on the constructor) are excluded — they are accepted appetite, not open debt. **2026-09-25 (owner delegated to the adversary):** `max_portfolio_risk_pct` (25), `SECTOR_HARD_CEILING_MAX` (90) and `max_cluster_risk_share_pct` (40) RATIFIED as owner-appetite (values unchanged, kept `status: arbitrary`+note). Item STAYS OPEN: `CLUSTER_CORRELATION_THRESHOLD` (0.7), `short_gap_risk_multiple` (1.5) and the queued-earnings BUY clamp (5%) are not yet resolved. **2026-09-26 pass — all three researched, none sourceable, all three refused rather than picked; item STAYS OPEN on three owner-appetite answers.** Findings, each recorded in the number ledger: (a) `CLUSTER_CORRELATION_THRESHOLD` — the definition site's claim that 0.7 is "the traditional finance cutoff" was UNTRUE and is deleted from the code, not softened. There is no such cutoff: the mainstream portfolio-clustering literature thresholds nothing, it clusters hierarchically on a correlation distance; where thresholded correlation networks are used the published cutoffs run ~0.3-0.8 and are picked for the network density a study wants. The old open question was also wrong — asking when this desk's names "actually fail together" is fitting a threshold to past outcomes, which doctrine bars. (b) `short_gap_risk_multiple` — the direction is arithmetic (a short's loss above its stop is unbounded, a long's is bounded by zero) and needs no citation; the magnitude is not sourceable and the literature that looks like it should settle it measures a different quantity, so it is NOT adopted: skewness-pricing work is about expected returns to lottery-like stocks, and the empirical overnight-gap studies are index-level and disagree in sign (the DJIA's larger median gap is on the UPSIDE but its skew is strongly negative, i.e. the fatter tail runs against longs). Measuring it properly is blocked on data, not thinking — the desk's database holds no OHLCV/bar table (verified 2026-09-26), bars are fetched live and discarded, so there is no stored gap history and no recorded short universe. (c) the queued-earnings BUY clamp — the near miss is written down so nobody adopts it later: the published ~5.07% average one-day absolute earnings-announcement return is a MOVE, this 5.0 is a share of the BOOK, and the two agreeing to two digits is a coincidence of units. Deriving it from the desk's own per-trade envelope fails too: run forward, a 5%-of-equity tolerance against a ~5.07% move would permit a weight near 100%, so the envelope does not bind here at all. Run backward it is a useful cross-check — today's 5% cap implies accepting ~0.25% of equity of unprotected overnight exposure, about half `min_position_risk_pct`, so the cap is conservative on the desk's own scale.


**2026-10-01 pass — the PORTFOLIO and CLUSTER ceilings are closed as far as they can honestly be closed: no value moved, no appetite routed, and each now carries a named recording plus BOTH of its failed derivations.** The three ratified ceilings (25 total at-risk, 90 terminal sector, 40 cluster share, plus the constructor's mirror of the 90) were re-checked against live code and against `config/number_ledger.yaml` first; all four were already ledgered and already re-affirmed as AGGREGATE rationing that the owner's "risk is never a global dial" ruling does not convert into a defect. What they did NOT have was the thing this item's closing condition actually asks for — a settlement route — so all four were sitting in the ledger's `unclassified` bucket, owing an answer with nothing named that could ever supply it. (a) EACH ROW'S APPETITE QUESTION IS WITHDRAWN, not pending: all three still asked the owner what concentration he accepts, which his 2026-09-30 ruling bars, and asking again is the failure mode this item has already suffered twice. (b) BOTH DERIVATIONS FAILED, PER CEILING, AND THE REASONS DIFFER. For the 25% book ceiling: no recording (the production `positions` table is an 11-row snapshot with no stop column and no history, so the book's loss-if-stopped has never been written down once — measured 2026-10-01 against /home/qamc/quant-agent/data/quant_agent.db, read-only), and THE ALGEBRA CANCELS (at the ratified 5% per-trade envelope a 25% ceiling is exactly five full-size names, so deriving it reduces to picking a name count). For the 90 terminal sector ceiling: THE ALGEBRA CANCELS (it is already a cap on a derivation, `min(1.5 x max_sector_pct, 90)`, so deriving it means deriving the 1.5 or the §12.3 target of 75, both unsourced), and no recording (sector is stored only on the snapshot; measured 2026-10-01 on production, the largest live sector share is 44.3% of gross market value against a 90 ceiling — one observation, not a distribution). For the 40% cluster share: THE ALGEBRA CANCELS, and this is the attempt that looked per-name and was not — 40% of 25% is 10% of equity, exactly TWO full-size positions at the 5% envelope, so the cluster share is a name count wearing a percent sign — and no recording (clusters are recomputed each session from live correlations and discarded; the production database has 24 tables and none of them holds a cluster). (c) THE RECORDINGS THAT CLOSE THEM are now written into the ledger rows themselves where the mechanical check reads them, with the route ratchet moved by -4 and the reason recorded: per-session book loss-if-stopped with the equity it was measured against; per-session sector shares of gross notional; per-session cluster membership from the correlation-distance cut with each cluster's share of total at-risk. Until those series exist the three ceilings stay `arbitrary` and stay at their ratified values. (d) A STALE DOCSTRING WAS CORRECTED ON SIGHT: `src/risk/budget.py` still told the reader clusters arrive "thresholded", which stopped being true when the 0.7 cutoff was removed on 2026-09-30, and still called the 40% "suggested". DO NOT RE-DERIVE these three: the loop has now failed twice on each, both reasons are written above, and in every case the second failure is circularity rather than missing data, so no new recording would rescue a derivation attempt — only a measurement of what the book actually does.


