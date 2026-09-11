"""Module-level risk constants shared across the pipeline + agent prompts.

Keeping these in one place avoids the failure mode where someone tightens
a threshold in one file (e.g., the force-delever trigger) but forgets the
corresponding prompt text that mentions the old number. Every code path
that cares about "is this account meaningfully on margin?" imports from
here.
"""

MARGIN_DEFICIT_FLOOR_USD = 1.0
"""Minimum cash deficit (in USD) before cash-only-policy actions fire.

Below this threshold, negative cash is treated as rounding noise — fill
rounding, commission leftovers, mid-price vs fill-price micro-drift —
that clears on the next reconcile pass. Triggering a force-sell for a
$0.30 deficit would be more disruptive than the phantom margin itself.

Consumers (must stay aligned — if you edit one, verify the others):
  - `TradingPipeline._force_delever`               (hard action threshold)
  - `PortfolioManagerAgent.build_user_message`     (DE-LEVER MANDATE prompt)
  - `PositionReviewerAgent.build_user_message`     (de-lever prompt in midday/close)
"""


REWARD_RISK_FLOOR = 1.5
"""Reward:risk a target must clear on the Technical read's own arithmetic
before it is sized normally.

Below it the payoff stops carrying an unproven hit rate — R/R X breaks even
at 1/(1+X), so 1.5 already needs to be right 40% of the time and this desk
has no measured per-setup hit rate to spend. A sub-floor idea is not
forbidden, but it must clear the catalyst gate and it is capped at
`STARTER_POSITION_RISK_PCT` (see `PortfolioManagerAgent._apply_subfloor_
catalyst_rule`).

Consumers (must stay aligned — if you edit one, verify the others):
  - `RiskConfig.min_reward_risk_after_widening`   (the constructor's gate,
                                                   which honours a verified
                                                   sub-floor catalyst — see
                                                   `TargetPosition.subfloor_
                                                   catalyst_verified`)
  - `PortfolioManagerAgent.decide`                (the PM-side default)
  - `config/prompts/portfolio_manager.md`         ("Adjust by Risk/Reward")
  - `config/prompts/risk_manager.md`              (`rr_fail` verdict)
"""

STARTER_POSITION_RISK_PCT = 0.5
"""The smallest position this desk will hold, as % of equity at risk.

Not a new number: it is `RiskConfig.min_position_risk_pct`, the floor
`allocate_risk_budget` already denies requests under. Anything smaller pays
full commission and full attention for an immaterial payoff, so a request
rationed below it is refused rather than shrunk — which is exactly why it is
also the right cap for a sub-floor catalyst trade: the smallest size the desk
can express without the idea being denied outright.

Consumers (must stay aligned — if you edit one, verify the others):
  - `RiskConfig.min_position_risk_pct`            (the budget floor)
  - `PortfolioManagerAgent.decide`                (the sub-floor cap default)
"""

DEFAULT_DRAWDOWN_VOL_SENSITIVITY = 3.0
"""How many multiples of the held book's own normal daily move trip a loss
alarm, before sqrt(time) window scaling. docs/WORK.md item 32.

PROVISIONAL AND REVERSIBLE — an owner risk-appetite decision made
2026-09-11, NOT a researched or validated number. There is no citable
industry-standard multiple for this; it was specifically researched and does
not exist. It replaced an earlier 6.7, which existed only so behaviour would
not jump when the alarms' basis changed, and which measurement then showed
meant the daily breaker fired only on a ~6.7-sigma session — a crash-grade
event, i.e. effectively dormant. 3.0 is roughly a 3% daily loss on a book
whose normal session is ~1%: a rough day, not a crash.

The sqrt(time) scaling this multiplies IS research-grounded and cited
(Van Hemert/Ganz/Harvey, "Drawdowns", JPM 2020). The multiple is not. Keep
that distinction in anything written about it.

Consumers (must stay aligned — if you edit one, verify the others):
  - `RiskConfig.drawdown_vol_sensitivity`         (the model default + full
                                                   reasoning)
  - `config/settings.yaml` (`risk.drawdown_vol_sensitivity`)
  - `TradingPipeline._build_agents` / `_compute_recent_performance`
    (the fallback used when settings cannot be read)
"""
