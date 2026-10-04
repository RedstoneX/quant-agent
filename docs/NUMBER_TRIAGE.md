# Unjustified-number triage (board item 90, 2026-10-01)

Sorts every ledger row the guard marks `arbitrary` into one of four piles. It adds the PILE and the RANK only: the route, the named recording and the per-row reasoning already live in `config/number_ledger.yaml` (`settles_by`) and `docs/board_notes/item-090.md`, and are not repeated here. No value was changed. Re-count with `src.number_sources.classification()`; this file is a reading of the ledger on 2026-10-01, not a gate.

## What was measured

- The ledger holds 336 rows [measured: `config/number_ledger.yaml`]; 133 are `arbitrary` -- the prior 133-of-336 figure is current [measured].
- 132 of the 133 `arbitrary` rows have an empty `source:` field; the justification text sits in `note` and `settles_by` instead [measured].
- No row in any other status has both `source` and `note` empty [measured].
- All 133 carry a settlement route (guard-enforced); 4 are routed to recordings already built [measured].

## Pile counts

| Pile | Rows | Meaning |
|---|---|---|
| SETTLEABLE | 64 | A mechanical measurement (stop-touch frequency, gap distribution, recorded refusals, public-bar statistics) answers it. |
| READ LIVE | 12 | Should be the instrument's own spread, tick, ATR or gap at decision time, not a stored number. |
| OWNER APPETITE | 12 | A loss-tolerance choice no measurement makes. The only rows that should reach the owner. |
| HARMLESS | 45 | Does not size, price or place an order. |

Rule for every pile: a number is settled by a mechanical measurement of how the market or the broker behaved, never by what would have made money on the desk's own record.

## Top five, measured 2026-10-01

The five highest-ranked SETTLEABLE rows (all tier 1, binding on every order) were measured; results sit under each row below as `Measured 2026-10-01`. Chosen because they set stop width, share count and the single-name ceiling on every trade: `min_stop_atr_multiple`, `absolute_min_stop_atr_multiple`, `max_position_risk_pct`, `max_position_pct`, `min_risk_pct` (with its twin `STARTER_POSITION_RISK_PCT`, one number). No value was changed.

Method, shared by all five: public daily bars, 2 years, for the 101-name tradable universe in `config/settings.yaml` (49,983 bar-days, 102 symbols incl. one held name outside the universe) [measured: yfinance, 2026-10-01]; the production database read-only for the desk's own entries (41 entries with a stop, 2026-09-02 to 2026-10-01) [measured]. ATR is 14-period; the stop-touch table used a simple true-range mean, the gap and entry-width tables Wilder's -- the repo measured the two to differ by a mean 7% (`src/data/technical.py`), so touch rates carry that tolerance. Nothing was fitted to profit or loss; every figure is a count of touches, gaps or widths.

## Ranking basis

Tier 1 numbers size a position or price a live order on EVERY trade; tier 2 gate entries or exits or shape stops after entry; tier 3 only shape which names reach the seats. Within a tier, a number that binds on every order outranks one that binds only in a tail event. Judgement, not a computed dollar figure [estimate: read from how each site is consumed].


## SETTLEABLE (64)

**Tier 1 sizes positions or prices/places live orders.** Stop-width floor and the regime/setup scales on it; same stop-touch-frequency measurement, split by regime and setup class (the regime-tagged recording is specified).

- `config.RiskConfig.absolute_min_stop_atr_multiple` = 1
- `portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[0][1]` = 1.2
- `portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[1][1]` = 1.1
- `portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[2][1]` = 0.95
- `portfolio_constructor.ConstructorConfig.stop_atr_setup_scale[1][1]` = 0.9

Measured 2026-10-01, `absolute_min_stop_atr_multiple` = 1 (the hard floor): on the universe, a stop k ATR below the close is touched within 10 sessions 61% of the time at k=1.0, 87% at k=0.25, 23% at k=2.5, 7% at k=4.0 (15,744 entry-days per row, every third day) [measured]. The whipsaw share of touches -- price closes back above the stop within 3 sessions -- is FLAT at 73-78% at EVERY distance from 0.25 to 4.0 ATR [measured]; there is no knee at 1.0 or anywhere else. The route's level-touch-count split was NOT done (needs the level engine), so the conditional knee it asks for is still unmeasured. Reading: distance alone does not separate whipsaw from trend break on daily bars; the 1.0 floor is neither vindicated nor falsified by this, and the remaining measurement is the level-conditioned one. On the desk's own 41 entries, 4 shipped with a stop inside 1 ATR (one above the entry price) and 0 sat exactly at 1.0, so the backstop the prompt says is applied in code either did not fire or the recorded `stop_loss` is the pre-floor value -- a recording-truth defect to settle before this number can be [measured].

**Tier 1 sizes positions or prices/places live orders.** Per-position size cap, loss-if-gapped-through bound and gross leverage; settled by the measured overnight gap distribution of the tradable universe. Survivability bound is measurable; only the tolerance above it is appetite.

- `config.RiskConfig.max_gross_exposure_x` = 2
- `config.RiskConfig.max_position_risk_pct` = 5
- `portfolio_constructor.ConstructorConfig.max_position_pct` = 65

Measured 2026-10-01, `max_position_risk_pct` = 5: a 2.5-ATR stop placed at the prior close is gapped through at the next open on 0.17% of bar-days (87 of 49,983) [measured]; when it is, the realised loss is a median 1.3x the planned risk, p90 2.0x, p99 2.75x, worst 2.92x [measured]. So the survivability half is settled: one name can lose up to ~2.9x its planned risk on daily bars, i.e. ~14.6% of the account at 5% planned risk. Whether 14.6% is tolerable is the owner's envelope, as the row already says. On the desk's record 0 of 41 entries requested or were allocated 5%; the maximum was 3.0% [measured], so the 5 has never bound. Survives as a ceiling that nothing has reached; its value stays unsettled until the owner states the whole-book envelope.

Measured 2026-10-01, `max_position_pct` = 65: pooled overnight gaps on the universe (49,881 gaps): worst 1-in-100 is -4.8%, 1-in-1,000 is -11.6%, 1-in-10,000 is -20.1%, worst single gap -41.1%; 75 gaps worse than -10%, 6 worse than -20% [measured]. A 65%-notional name at those gaps costs the account 3.1% / 7.6% / 13.0% / 26.7% [measured arithmetic]. The desk's largest live name is 19.9% of the book, average 9.1% (11 positions) [measured], so 65 has never bound. The measurement half is done; the ceiling closes only when the owner names the single-name no-fill loss he will accept (board item 186) and picks the percentile.

**Tier 1 sizes positions or prices/places live orders.** Target-reach cap in ATR; settled by realised travel over each holding length on public bars.

- `config.RiskConfig.max_target_reach_atr_multiple` = 1.5

**Tier 1 sizes positions or prices/places live orders.** Sets stop width, and therefore share count, on every position. Settled by how often a stop at each ATR multiple was touched on public daily bars, plus the per-trade adverse-excursion recording already built.

- `config.RiskConfig.min_stop_atr_multiple` = 2.5

Measured 2026-10-01: the built recording is still EMPTY (entry ATR on 3 of 84 trade rows, adverse excursion on 1) [measured], so it settled nothing; the width was recomputed instead from public bars at each entry date. Of the desk's 41 entries, stop width in ATR was min -0.4, p25 1.5, median 2.1, p75 2.4, max 4.5; 34 sat below 2.5, 2 within 0.05 of it, 5 above; 17 fell inside the 2.14-3.00 push-out band [measured]. That is consistent with the prompt's rule that a level-backed stop is honoured as placed, but `stop_level_basis` is written on only 3 rows, so level-backed and floor-bypassed cannot be told apart -- the desk never recorded which stops the floor acted on. On the universe a 2.5-ATR stop is touched within 10 sessions 23% of the time and within 20 sessions 38% [measured]; a 1.5-ATR stop 45% and 58%. The floor is not falsified (nothing in the touch table says 2.5 is wrong) and not vindicated (the floor-violation question cannot be asked until `stop_basis` and `entry_atr` populate); the actionable finding is the recording gap.

**Tier 1 sizes positions or prices/places live orders.** Minimum and starter risk floor; a built recording counts every order the floor refuses, which settles whether the floor ever binds.

- `portfolio_constructor.ConstructorConfig.min_risk_pct` = 0.5
- `risk.constants.STARTER_POSITION_RISK_PCT` = 0.5

Measured 2026-10-01: the item-223 recording has written 0 rows -- `trade_refusals` holds 14 rows, all reward-to-risk refusals from the parity gate, and `requested_risk_pct` is null on every one [measured]. On the 41 entries with a recorded request, 0 asked for less than 0.5%; 8 asked for 0.5-1.0% (minimum allocated 0.5%), 15 for 1-2%, 18 for 2-3% [measured]. The floor has never refused anything and the seat's lowest request equals the floor, so the number currently governs nothing; the route's one-year clock for reformulation is the right close and is running. Survives, unexercised.

**Tier 1 sizes positions or prices/places live orders.** Refusal gate on every buy; settled from the reward-to-risk of every nominated name on public bars, never from the desk's own fills.

- `risk.constants.REWARD_RISK_FLOOR` = 1.5

**Tier 2 gates entries/exits or shapes stops after entry.** Order-floor, target horizon, sector common drawdown, earnings stance age, event horizon, pre-filter spread, and stop-placement retry ceiling and backoff; each has a named recording or public-data measurement.

- `config.CashSweepConfig.min_order_usd` = 500
- `config.EventRiskConfig.horizon_days` = 10
- `config.RiskConfig.max_target_horizon_sessions` = 60
- `portfolio_constructor.ConstructorConfig.max_sector_pct` = 75
- `risk.rules.EARNINGS_STANCE_MAX_AGE_DAYS` = 90
- `execution.broker._STOP_PLACEMENT_MAX_ATTEMPTS` = 3
- `execution.broker._STOP_PLACEMENT_BACKOFF_S[0]` = 0.5
- `execution.broker._STOP_PLACEMENT_BACKOFF_S[1]` = 1.5
- `pipeline_risk_gate.RiskGate._has_actionable_signal_fn:factor[0]` = 0.5

**Tier 2 gates entries/exits or shapes stops after entry.** Cash reserve and deployment band; settled by recorded assumed-vs-debited cash and by the measured round-trip cost of trading the gap.

- `config.CashReserveConfig.pct` = 1
- `config.DeploymentGapConfig.band_pct` = 1

**Tier 2 gates entries/exits or shapes stops after entry.** Touches needed before a level is honoured as a stop; settled by bounce rate per touch count on public bars.

- `config.RiskConfig.min_level_touches_for_stop_honor` = 5

**Tier 2 gates entries/exits or shapes stops after entry.** Exit and trailing-stop thresholds; settled by public-bar retracement/trend-resumption statistics and by recorded session-to-session noise of the exit guard's own inputs.

- `risk.alignment_exit.ALIGNMENT_GIVE_BACK_ATR_MULTIPLE` = 3.0
- `risk.exit_guard.NOISE_BAND_ATR_MULTIPLE` = 1
- `risk.exit_guard.BREAK_CONFIRMATION_ATR_MULTIPLE` = 1
- `risk.exit_guard._NOISE_FLOOR['distance_to_stop_pct']` = 0.1
- `risk.exit_guard._NOISE_FLOOR['pace']` = 0.05
- `risk.exit_guard._NOISE_FLOOR['r_multiple']` = 0.05
- `risk.exit_guard._NOISE_FLOOR['thesis_progress_pct']` = 0.5
- `risk.trailing.NOISE_BAND_ATR_MULTIPLE` = 1.25
- `risk.trailing.RANGE_BREAKEVEN_R_MULTIPLE` = 1
- `risk.trailing.RANGE_SECOND_RATCHET_TRIGGER_R` = 2
- `risk.trailing.RANGE_SECOND_RATCHET_LOCK_R` = 1
- `exits.exit_records.ExitRecords._trail_tightened_recently(calendar_days)` = 4

**Tier 2 gates entries/exits or shapes stops after entry.** Rotation margin and seat weights; settled by measured ranking-score noise and each seat's forward discriminating power on public data.

- `rotation.ROTATION_MARGIN_PCT` = 0.25
- `verdicts.SEAT_WEIGHT['earnings']` = 1.2
- `verdicts.SEAT_WEIGHT['macro']` = 0.8
- `verdicts.SEAT_WEIGHT['smart_money']` = 0.8
- `verdicts.SEAT_WEIGHT['technical']` = 1.2

**Tier 3 shapes which names reach the seats.** Signal-classification windows, candidate caps and scan cadence; settled from filing-date and screen-demand recordings. They shape which names reach the seats, not size or stops.

- `config.IntradayScanConfig.cooldown_hours` = 3
- `config.IntradayScanConfig.max_candidates_per_scan` = 5
- `config.NominationConfig.max_per_seat_per_run` = 3
- `config.NominationConfig.max_total_per_run` = 6
- `config.SmartMoneyConfig.cluster_window_days` = 2
- `config.SmartMoneyConfig.congress_lookback_days` = 180
- `config.SmartMoneyConfig.insider_cadence_max_gap_dispersion` = 0.25
- `config.SmartMoneyConfig.insider_cadence_max_mean_gap_days` = 120
- `config.SmartMoneyConfig.insider_cadence_min_mean_gap_days` = 20
- `config.SmartMoneyConfig.insider_calendar_routine_years` = 3
- `config.SmartMoneyConfig.insider_min_cadence_trades` = 3
- `config.SmartMoneyConfig.lookback_days` = 365
- `config.SmartMoneyConfig.max_external_candidates` = 3
- `config.SmartMoneyConfig.max_observations` = 40
- `config.SmartMoneyConfig.min_cluster_owners` = 2
- `config.SmartMoneyConfig.min_external_history_days` = 20
- `data.levels.PIVOT_WINDOW` = 5
- `data.smart_money_cluster.MAX_CLUSTER_RESERVED_SLOTS` = 5
- `data.smart_money_cluster.MIN_PURCHASE_CLUSTER_INSIDERS` = 2
- `risk.trailing.PIVOT_WINDOW` = 3
- `agents.smart_money_analyst._MAX_SYNTHESIS_SYMBOLS` = 8
- `agents.tech_analyst._BARS_PER_SYMBOL` = 40

## READ LIVE (12)

**Tier 1 sizes positions or prices/places live orders.** Entry slippage ceiling: should be the live quoted spread at the order, not 40 bps.

- `config.ExecutionConfig.max_entry_slippage_bps` = 40

**Tier 1 sizes positions or prices/places live orders.** Limit pads on exits, entries and stop-limits: should come from the live bid-ask spread, tick size and the name's own gap distribution.

- `execution.cash_sweep._SELL_LIMIT_PAD` = 0.999
- `execution.broker.AlpacaBroker.STOP_LIMIT_BUFFER_PCT` = 0.03
- `stage_execution.ExecutionStage._run_session:factor[0]` = 0.995

**Tier 2 gates entries/exits or shapes stops after entry.** Minimum ratchet step and short-side gap multiple: should be tick/ATR-relative and the name's own gap distribution.

- `risk.constants.SHORT_GAP_RISK_MULTIPLE_DEFAULT` = 1.5
- `risk.trailing.MIN_RATCHET_TICKS` = 1 (RESOLVED 2026-10-02: the 2% floor is retired; the minimum ratchet step is now one venue tick, read off the instrument)

**Tier 2 gates entries/exits or shapes stops after entry.** Stop buffer, level-cluster tolerance and level-strength distance: percent constants that should be the name's own ATR or tick.

- `data.levels.CLUSTER_TOLERANCE_PCT_FALLBACK` = 1
- `data.levels.LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT` = 10
- `portfolio_constructor.ConstructorConfig.structural_stop_buffer_pct` = 0.005

**Tier 3 shapes which names reach the seats.** Intraday move trigger, external-candidate dollar-volume and price floors: should be multiples of the name's own ATR, spread and tick.

- `config.IntradayScanConfig.move_threshold_pct` = 3
- `config.SmartMoneyConfig.min_external_avg_dollar_volume_usd` = 10000000
- `config.SmartMoneyConfig.min_external_price_usd` = 5

## OWNER APPETITE (12)

Portfolio loss tolerance, concentration ceilings, per-trade risk budget and the drawdown ladder: how much loss the owner will bear is a preference. A measurement can bound the maximum survivable value, not choose it.

- `config.RiskConfig.SECTOR_HARD_CEILING_MAX` = 90
- `config.RiskConfig.max_cluster_risk_share_pct` = 40
- `config.RiskConfig.max_portfolio_risk_pct` = 25
- `portfolio_constructor.ConstructorConfig.max_sector_hard_pct` = 90
- `portfolio_constructor.ConstructorConfig.risk_budget_pct` = 5
- `risk.rules.GROSS_LADDER[0][0]` = -8
- `risk.rules.GROSS_LADDER[0][1]` = 1.5
- `risk.rules.GROSS_LADDER[1][0]` = -15
- `risk.rules.GROSS_LADDER[1][1]` = 1
- `risk.rules.GROSS_LADDER[2][0]` = -20
- `risk.rules.GROSS_LADDER[2][1]` = 0.5
- `risk.rules.RiskRuleEngine.check(max_correlated_cluster_pct)` = 50

## HARMLESS (45)

Warn-only flag, deadline, fetch/text caps, ordinal sort-key ranks, JSON-candidate scoring weights and prompt-block row limits: none sizes, prices or places an order.

- `config.RiskConfig.target_divergence_warn_pct` = 25
- `config.SmartMoneyConfig.max_filings_per_refresh` = 1000
- `config.SmartMoneyConfig.refresh_deadline_s` = 180
- `agents.smart_money_analyst._MAX_REPRESENTATIVE_TRANSACTIONS` = 3
- `agents.smart_money_analyst._MAX_FINDING_TEXT_WORDS` = 24
- `agents.smart_money_analyst._MAX_CONTEXT_TEXT_CHARS` = 96
- `agents.smart_money_analyst._MAX_REASON_TEXT_CHARS` = 220
- `agents.smart_money_analyst._MAX_ACTOR_ROLES` = 8
- `agents.smart_money_analyst._ROLE_RANK['actionable']` = 3
- `agents.smart_money_analyst._ROLE_RANK['confirmatory']` = 2
- `agents.smart_money_analyst._ROLE_RANK['contradictory']` = 1
- `agents.smart_money_analyst._FRESHNESS_RANK['fresh']` = 2
- `agents.smart_money_analyst._FRESHNESS_RANK['delayed']` = 1
- `agents.smart_money_analyst._SIGNAL_CLASS_RANK['opportunistic']` = 2
- `agents.smart_money_analyst._SIGNAL_CLASS_RANK['']` = 1
- `agents.smart_money_analyst._SIGNAL_CLASS_RANK['indeterminate']` = 1
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['actions']` = 50
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['analyses']` = 40
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['approved']` = 50
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['daily_summary']` = 40
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['decisions']` = 50
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['findings']` = 50
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['investment_implications']` = 40
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['macro_narrative']` = 40
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['portfolio_view']` = 20
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['rating']` = 5
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['reasoning_chain']` = 20
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['regime']` = 40
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['symbol']` = 5
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['targets']` = 50
- `agents.base.AgentResult._EXPECTED_AGENT_KEY_WEIGHTS['tomorrow_outlook']` = 40
- `pipeline_prompt_facts.PromptFactsMixin._build_blocked_proposals(lookback_days)` = 21
- `pipeline_prompt_facts.PromptFactsMixin._build_blocked_proposals(max_lines)` = 5
- `pipeline_prompt_facts.PromptFactsMixin._build_blocked_proposals(min_proposals)` = 3
- `pipeline_prompt_facts.PromptFactsMixin._build_calibration_note(lookback_days)` = 45
- `pipeline_prompt_facts.PromptFactsMixin._build_missed_opportunities_digest(min_top_mover_dollar_volume_m)` = 5
- `pipeline_prompt_facts.PromptFactsMixin._build_missed_opportunities_digest(top_movers_count)` = 15
- `pipeline_prompt_facts.PromptFactsMixin._build_own_recent_decisions(limit)` = 3
- `pipeline_prompt_facts.PromptFactsMixin._build_pm_recent_decisions(limit)` = 3
- `pipeline_prompt_facts.PromptFactsMixin._build_post_exit_reality(max_symbols)` = 12
- `pipeline_prompt_facts.PromptFactsMixin._build_post_exit_reality(min_age_days)` = 2
- `pipeline_prompt_facts.PromptFactsMixin._build_recent_loss_pits(lookback_days)` = 14
- `pipeline_prompt_facts.PromptFactsMixin._build_recent_missed_lessons(lookback_days)` = 14
- `pipeline_prompt_facts.PromptFactsMixin._build_rm_recent_verdicts(limit)` = 5
- `pipeline_prompt_facts.PromptFactsMixin._build_thesis_health_context(lookback_weeks)` = 8
