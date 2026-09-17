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


BREAKOUT_SETUP_TYPE = "breakout"
"""`TechAnalysisResult.setup_type` for a Type B / trend trade.

The one string this desk uses for "no overhead structure". Mirrors
`src/risk/trailing.py`, which already branches on exactly this literal
(`setup_type != "breakout"` is Type A there) — the same convention, not a
second one. Anything else, including None and an unrecognised value, is
treated as Type A / range, which is the conservative side: the reward:risk
machinery below keeps applying.
"""


def is_trend_trade(
    setup_type: str | None, *, structural_ceiling: bool | None = None,
) -> bool:
    """Is there nothing overhead that is expected to stop this trade?

    **One definition, two consumers, on purpose** (2026-09-11, docs/WORK.md
    item 1(d) plus funnel item 6). The same real-world condition decides both
    of these, and they must not be able to drift apart:

      * whether any reward:risk comparison applies at all
        (`reward_risk_floor_applies` below);
      * whether `src/data/levels.py::derive_structural_target` may use its
        ATR measured-move projection instead of refusing for want of a level.

    Two independent ways to know it, and either is sufficient:

      * **The label.** The Technical Analyst typed `setup_type="breakout"`.
        This is the desk's existing Type A/B distinction and the same literal
        `src/risk/trailing.py` already branches on.
      * **The measurement.** `structural_ceiling=False` — the desk's OWN
        level computation found no structural level in the trade's direction
        of travel, on a chart that DID yield levels elsewhere. That is a
        computed fact about this specific trade, not an assertion about it,
        and it is strictly better evidence than the label.

    `structural_ceiling=None` means "not computed at this call site", not
    "no ceiling" — so a caller that cannot measure it falls back to the label
    alone, which is the conservative side.

    Why the measurement half exists: funnel item 6 ("no structural level from
    which to derive a target") refused real trades whose charts genuinely had
    nothing overhead, purely because the analyst had not separately typed the
    word "breakout" — while the correct ATR projection for exactly that case
    already existed in the code, reachable only through the label.
    """
    if str(setup_type or "").strip().lower() == BREAKOUT_SETUP_TYPE:
        return True
    return structural_ceiling is False


def reward_risk_floor_applies(
    setup_type: str | None, *, structural_ceiling: bool | None = None,
) -> bool:
    """Does any reward:risk comparison apply to this trade at all?

    **False for a Type B / breakout trade, and that is the whole rule**
    (docs/WORK.md item 1 part (d), owner decision 2026-09-11).

    A breakout has no overhead level anyone is defending — `src/risk/
    trailing.py`'s module docstring is the desk's own statement of this, and
    the position is MANAGED accordingly: trailed from entry, with no fixed
    profit target at all. Any "reward" put in the numerator of a ratio for
    such a trade is therefore a number invented to satisfy the ratio, not a
    price the desk is trading toward. Gating on it, or sizing on it, judged
    the trade against a target its own exit management never intended to
    reach. Approval for a Type B trade rests on the RISK side alone: a real
    level-backed or ATR-derived stop, plus the multi-agent conviction and
    evidence checks that run regardless of setup type.

    True for a Type A / range trade, whose reward:risk IS real — measured
    from that specific trade's own support (risk) and resistance (reward).
    What is done with that number: ranking, not a cutoff, and not a size
    cap (owner 2026-09-17). An unmeasurable range payoff still fails closed
    because there is no arithmetic at all, which is not this comparison.

    Fails to the conservative side: an unknown or missing setup type, with
    no measured ceiling fact supplied, keeps the reward:risk machinery on.

    `structural_ceiling` is the measured half — see `is_trend_trade`. A
    caller that has run the desk's own level computation for this trade can
    pass `False` when nothing sits in its direction of travel, and the
    exemption then rests on that fact rather than on the analyst's wording.
    """
    return not is_trend_trade(
        setup_type, structural_ceiling=structural_ceiling,
    )


REWARD_RISK_FLOOR = 1.5
"""Retired numeric reward:risk threshold. **Not a pass/fail gate and not a
size cap anywhere** (owner 2026-09-17: invented R/R leftovers are a
defect; 2026-09-11 item 1(d) had already stopped using it as a refusal).

Kept only as the default of the historical `RiskConfig.min_reward_risk_
after_widening` settings key so a silent rename cannot drop a deployed
threshold, and as a name tests/docs still import. Nothing in the live
entry path compares a ticket against this number to refuse or shrink it.

A Type B / breakout trade is not measured on reward:risk at all
(`reward_risk_floor_applies`). A Type A / range trade's real per-trade
ratio is still computed and still reaches `src/verdicts.py::rank_verdicts`
as an ordering signal. An UNMEASURABLE range payoff (cannot compute the
ratio at all) is still refused — that is honesty about unknown geometry,
not this number.

The arithmetic that once justified 1.5 — R/R X breaks even at 1/(1+X) —
is still true and still unused: this desk has no measured per-setup hit
rate to spend.

Consumers (must stay aligned — if you edit one, verify the others):
  - `RiskConfig.min_reward_risk_after_widening`   (inert historical key;
                                                   default only)
  - `config/prompts/portfolio_manager.md`         (ranking signal, not
                                                   a Python cap)
  - `config/prompts/risk_manager.md`              (must not refuse on it)
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


