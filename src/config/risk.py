"""Risk settings: position sizing, stops, cash sweep/reserve, and scheduled-event risk.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator
from src.risk.constants import (
    SHORT_GAP_RISK_MULTIPLE_DEFAULT,
    STARTER_POSITION_RISK_PCT,
)


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_total_position_pct: float = Field(gt=0)
    max_sector_pct: float = Field(gt=0, le=100)
    # Spec §10.3 (owner-ratified 2026-09-01). `max_sector_pct` above is no
    # longer a veto — it is the diversification TARGET, past which further
    # trades in that sector are progressively SHRUNK rather than refused
    # (`src/risk/rules.py::sector_size_scale`). This is the absolute ceiling
    # the shrinking runs into, past which the answer is still no. Without it
    # a sector could grow without limit through ever-smaller additions.
    #
    # Default is 1.5x the target, capped at `SECTOR_HARD_CEILING_MAX` (90,
    # spec §12.3), deriving from `max_sector_pct` rather than hard-coding a
    # number so that an operator who tightens or loosens the target moves the
    # ceiling with it instead of silently leaving the two inconsistent. The
    # cap exists because 1.5x an already-permissive target stops being a
    # ceiling: at the §12.3 target of 75 it would give 112.5.
    max_sector_hard_pct: float | None = Field(default=None, gt=0, le=100)
    require_stop_loss: bool
    # Owner-ratified total at-risk ceiling (2026-08-27): the sum of every
    # position's loss-if-stopped, measured against cost basis, may not exceed
    # this share of equity. Distinct from `max_total_position_pct`, which caps
    # NOTIONAL: a $50k book with 10% stops is 50% invested and 5% at risk.
    # Capital is meant to be fully deployed; it is RISK that is rationed.
    #
    # Reporting-only today — `PMFacts` renders the figure and its headroom so
    # the Portfolio Manager sizes against a real number instead of a rule it
    # was told about but never shown. Phase 2b makes it a hard gate.
    max_portfolio_risk_pct: float = Field(default=25.0, gt=0, le=100)
    # Spec §2.1. The owner-ratified per-trade envelope (2026-08-27). Conviction
    # is expressed as the share of equity an idea may lose if its stop is hit,
    # and the constructor derives share count from it:
    #     shares = (equity x risk_pct / 100) / |entry - stop|
    # A wider stop therefore yields a SMALLER position rather than a rejected
    # trade, which is what removes the incentive to squeeze stops. The prior
    # 0.5% ceiling lived in a constructor dataclass default nobody chose.
    max_position_risk_pct: float = Field(default=5.0, gt=0, le=100)
    # Below this an idea is not worth trading: a token position pays full
    # commission and full attention for an immaterial payoff. A request
    # rationed under the floor is denied outright rather than shrunk.
    min_position_risk_pct: float = Field(
        default=STARTER_POSITION_RISK_PCT, ge=0, le=100,
    )
    # Spec §2.2. The most of the total at-risk ceiling any ONE correlated
    # cluster may take. Without it "total risk is under 25%" says nothing
    # about diversification — a book holding one theme four times over
    # satisfies it while carrying exactly the concentration the ceiling
    # exists to prevent. Correlated names consume one bet's budget.
    max_cluster_risk_share_pct: float = Field(default=40.0, gt=0, le=100)
    # Minimum stop distance in ATRs. Structure places the stop; this only
    # pushes it out when structure put it inside ordinary volatility. Measured
    # 2026-08-27: stops sat a median 4.3% below entry against a median ATR of
    # 2.56% of price — about 1.7 ATRs, barely more than one ordinary day's
    # range, which is what was firing exits inside noise AND forcing enormous
    # positions to reach any meaningful risk.
    #
    # 3.0 -> 1.5 -> 2.5 (2026-09-10). The 1.5 came from this desk's own
    # ~2-week MAE sample — later found to overlap the window whose seat
    # outputs were misreporting confidence/data quality, so no longer trusted
    # as the sole basis. 2.5 comes from published swing-trading doctrine
    # instead (2.5-3.0x ATR for a fixed entry stop on a multi-day hold),
    # independent of this desk's own data. Only applies with no real level
    # backing the stop — a level-backed stop is judged on its own honest
    # distance regardless of this number. Full derivation and caveats:
    # `config/settings.yaml` (this key) and docs/INCIDENT_HISTORY.md
    # 2026-09-10. Keep the three in sync.
    #
    # 2026-09-30, board item 90 — value UNCHANGED. Reasons live in
    # `config/number_ledger.yaml` under this id rather than copied here;
    # two corrections belong at the definition site itself.
    #
    # FIRST, the "3.0 -> 1.5 -> 2.5 (2026-09-10)" line above is CORRECT and
    # is confirmed here because its counterpart in config/settings.yaml is
    # not: that file says the first leg happened on 2026-09-04, and no such
    # move exists. Read from git, the DEPLOYED value went 3.0 (3dff9408,
    # 2026-08-27) straight to 2.5 (0088328c, 2026-09-10) and was never 1.5
    # — `git log --all -S'min_stop_atr_multiple: 1.5' -- config/settings.yaml`
    # returns nothing. 0088328c squashes PR #269, whose sub-commit one moved
    # 3.0 -> 1.5 and whose sub-commit two moved 1.5 -> 2.5 on the same day;
    # the 1.5 never left that branch. Both legs, one merge, one date.
    #
    # SECOND, "2.5 comes from published swing-trading doctrine" is NOT a
    # citation and this number is not sourced. docs/OUTCOME.md requires a
    # URL a later reader can open. Searched 2026-09-30: no such source
    # exists for a FIXED entry stop, the corroborating 2-3x figures are
    # trailing-stop mechanisms (which the comment below already concedes),
    # and one secondary claim points the other way entirely. Both ends of
    # the quoted band are unsupported, not merely the point inside it.
    # 2.5 is therefore the INTERIM value and is deliberately not re-picked:
    # with no cited band, moving it would be one more unsourced choice.
    # The route that removes this constant for most names it governs is
    # board item 199 (read the floor off the nearest computed level rather
    # than off an ATR multiple).
    min_stop_atr_multiple: float = Field(default=2.5, gt=0, le=10)
    # NO `min_reward_risk_after_widening` HERE ANY MORE — removed 2026-09-24
    # (board item 81). It refused nothing and capped nothing: no code in
    # `PortfolioConstructor` ever read `self.min_reward_risk_after_widening`,
    # and the one place the value was threaded to
    # (`PortfolioManagerAgent._apply_subfloor_catalyst_rule`) explicitly
    # discards it. Removed keys are rejected loudly by
    # `_reject_deleted_reward_risk_floor_key` below.
    # --- Level-backed stops (spec §12.1, 2026-09-01) ---------------------
    # `min_stop_atr_multiple` above used to OVERWRITE the structural stop
    # whenever the level sat closer than the band, after which the stop was
    # at nothing real. On 2026-09-01 the desk reviewed 38 qualified
    # signals and placed zero trades. A stop that sits at a level
    # `src/data/levels.py::find_structural_levels` actually computed is now
    # honoured whatever its ATR distance; the band only applies when nothing
    # computed backs it.
    #
    # NO `level_match_atr_tolerance` HERE ANY MORE — removed 2026-09-13,
    # docs/WORK.md item 46. It was 0.25 ATR and justified itself as being "at
    # least as wide" as the 1% zone `find_structural_levels` clusters pivots
    # into. Those are different units, so the claim was only ever true above
    # a particular volatility: 0.25 x ATR >= 0.01 x price needs ATR >= 4% of
    # price. At the 2.56%-of-price median ATR this repo's own comments quote,
    # 0.25 ATR is 0.64% — 1.56x NARROWER than the zone it claimed to cover,
    # so a stop sitting inside a level's real zone was not counted as sitting
    # at that level. There is no honest ATR multiple to replace it with: the
    # zone is defined as a percentage of price, the tolerance was a multiple
    # of ATR, and the ratio between them changes with every name on every
    # day. It is not replaced by a different constant — it is deleted, and
    # "is this stop AT this level" now reads the zone's own bound from
    # `src.data.levels.level_zone_halfwidth`, which derives it from the same
    # `CLUSTER_TOLERANCE_PCT` that built the zone. The two can no longer
    # disagree because there is only one of them.
    #
    # The ATR argument the old comment made is not lost, it was misplaced:
    # "is this stop far enough out to survive the name's noise" IS an ATR
    # question, and it is already asked, deterministically, by
    # `min_stop_atr_multiple` and `absolute_min_stop_atr_multiple` below.
    # "Which level is this stop sitting on" is an identity question about a
    # zone, and is answered in the zone's own unit.
    # The deterministic backstop under the exemption above. §12.1's safety
    # argument rests on the 1*ATR hard floor in
    # `config/prompts/tech_analyst.md` — but that is a PROMPT, and Invariant
    # 2 requires deterministic Python protections to be the final authority
    # and to fail closed. A real support level 0.2 ATR under entry is genuine
    # structure AND a guaranteed whipsaw. So a level-backed stop is honoured
    # however tight down to this many ATRs; inside it the stop is pushed out
    # to exactly this floor — never to the full `min_stop_atr_multiple` band.
    absolute_min_stop_atr_multiple: float = Field(default=1.0, ge=0, le=10)
    # How many prior touches a computed level needs before a stop sitting on
    # it is trusted enough to be honoured however tight (Phase 12.1,
    # 2026-09-03 — docs/RESEARCH_FINDINGS.md §7). `find_structural_levels`
    # already requires 2 touches to register a level at all (`MIN_TOUCHES`),
    # but §12.1's own text names that as a SEPARATE, undecided question: "a
    # level currently qualifies on two touches ever ... so two old swing
    # points can justify a very tight stop." The measured table (101
    # symbols, 5 years, daily bars, real vs shuffled arithmetic control)
    # only clears real-vs-shuffled separation cleanly at 5+ touches: real
    # 0.644 [0.590, 0.696] against shuffled 0.505 [0.470, 0.539] — the
    # confidence intervals do not overlap at all. Every lower bucket's
    # intervals overlap or nearly touch (2 touches: real floor 0.510 versus
    # shuffled ceiling 0.510; 3 and 4 touches overlap outright), so a bar
    # below 5 would be honouring a tight stop on a separation that could be
    # noise. Below this bar the stop is NOT treated as level-backed and
    # falls back to `min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`
    # exactly as an unbacked stop does — it does not become untradeable, it
    # loses only the tight-stop exemption.
    min_level_touches_for_stop_honor: int = Field(default=5, ge=1, le=20)
    # --- Target derivation (2026-09-01) ---------------------------------
    # The floor above was dividing a stop computed from measured volatility
    # by a target a language model guessed. On 2026-09-01's morning run that
    # rejected 30 of 38 actionable signals (79%) before any judgement was
    # applied, the two highest-conviction calls among them. The floor is not
    # the defect; its numerator was. These tune the deterministic target
    # derivation that replaced it — see the target-derivation section of
    # src/data/levels.py for the rule and the arithmetic.
    #
    # Flags a target inside this many ATRs of entry as thin reward — the
    # whole payoff sits inside one ordinary session's range. It LABELS the
    # derived target and never selects it (changed 2026-09-30; it used to
    # drop such a level and take the next one out, past real structure).
    min_target_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # Measured move claimed when no structural level stands in the way, in
    # sqrt(session)-scaled ATRs. 1.0 = the typical excursion over the stated
    # horizon. NOTE the interaction with `min_stop_atr_multiple`: a stop at
    # k ATRs and a target at p*ATR*sqrt(H) clear a floor f only when
    # sqrt(H) >= f*k/p — at k=3.0, p=1.0, f=1.5 that is H >= ~21 sessions.
    breakout_projection_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # How far price can plausibly travel within the horizon, same units.
    # Looser than the projection on purpose: this asks "could it get there",
    # the projection asks "how far do I claim it goes".
    max_target_reach_atr_multiple: float = Field(default=1.5, gt=0, le=5)
    # NO `max_stop_width_reach_atr_multiple` HERE ANY MORE -- the stop-width
    # REFUSAL it threshold-ed was deleted 2026-09-26 (board item 56, route
    # (c)). It was split off from `max_target_reach_atr_multiple` on
    # 2026-09-13 so that estimating a target and refusing a trade stopped
    # sharing one number; the split made them independent without making
    # either derived, and no published work fixes the touch probability
    # below which a stop stops being a stop. Measured before deletion: 648
    # sized stops recorded a touch-probability reading in production
    # (quant_agent.log, 2026-09-13..2026-09-26) and the refusal fired zero
    # times; the widest stop ever seen was 1.29 x ATR x sqrt(H) against a
    # 1.5 cap. A wide stop is answered by a smaller position
    # (`_plan_risk_targets`, the ratified spec 2.1 invariant) and, at the
    # extreme, by `position_sized_to_zero`. Removed keys are rejected loudly
    # by `_reject_deleted_stop_width_gate_key` below. The target-side
    # `max_target_reach_atr_multiple` is UNAFFECTED and still in force.
    # Ceiling on `expected_horizon_sessions` before it enters the sqrt()
    # travel estimate, so an implausible horizon cannot licence a target far
    # outside anything the symbol does.
    max_target_horizon_sessions: int = Field(default=60, ge=1, le=500)
    # Absolute gap between the computed target and the analyst's guess above
    # which the disagreement is logged at WARNING. The guess is kept as
    # evidence, never as arithmetic.
    target_divergence_warn_pct: float = Field(default=25.0, gt=0, le=200)
    # Cash-only default. When False: no BUY may drive `cash` below zero, and
    # any session that starts with `cash < 0` must de-lever (SELL) before any
    # new BUY. When True: normal margin account behavior, risk engine only
    # enforces the exposure / sector / loss caps. Default False is the
    # conservative choice — margin leverage amplifies drawdowns and is not
    # the bot's intended mode unless explicitly opted in.
    allow_margin: bool = False
    # --- Margin interest tracker (spec §11.2, 2026-09-01) ----------------
    # MEASURES, does not gate — this field feeds an estimate/alert only,
    # never a risk check. Alpaca's live non-elite margin rate (elite is
    # 4.75%); a config value rather than a code constant so the desk can
    # correct it without a deploy if Alpaca's rate moves. Interest accrues
    # ONLY on the END-OF-DAY (overnight) debit balance — intraday leverage
    # is free — per `(overnight debit balance x rate) / 360`. See
    # src/margin_interest.py: whether PAPER trading actually charges
    # this is UNCONFIRMED (Alpaca's own comparison lists short-borrow fees
    # as "Coming Soon" and is silent on margin interest either way), so
    # every figure this produces is a labelled ESTIMATE until the broker's
    # own `INT` account activity settles it empirically.
    margin_interest_rate_pct: float = Field(default=6.25, ge=0, le=100)
    # --- Spec §11.2: the gross-exposure ceiling (owner-ratified 2026-09-01)
    #
    # Gross exposure = long market value + ABSOLUTE short market value,
    # measured against equity. Before this setting existed the codebase had
    # NO gross-exposure ceiling of any kind: `max_portfolio_risk_pct` bounds
    # AT-RISK capital (the sum of stop distances), not exposure. Nothing stopped the book reaching the
    # broker's full 4x. Adding this is a TIGHTENING, not a loosening.
    #
    # 2.0x is the owner's deliberate paper-account learning setting, taken
    # against the recommendation to defer — see the §11.2 spec entry and
    # [[qamc-live-capital-checklist]]. Re-derive it before real money.
    #
    # This is the STANDING cap, day AND night. There is deliberately no
    # separate, lower overnight ceiling: an intraday-only allowance would
    # force a trim into every close, selling on a clock rather than on merit,
    # and this desk holds for days so it would almost never use one. The
    # overnight cushion comes from the de-levering ladder
    # (`src/risk/rules.py::resolve_gross_ceiling`) instead.
    #
    # The ladder can only ever tighten this number, never raise it — so
    # lowering this setting lowers every rung with it.
    max_gross_exposure_x: float = Field(default=2.0, gt=0, le=4.0)
    # Broker maintenance-margin requirement, as a percent of gross exposure,
    # used ONLY to report distance-to-forced-liquidation
    # (`src/risk/rules.py::distance_to_forced_liquidation_pct`). It computes
    # nothing the engine enforces; it answers "how far could the book fall
    # before the broker sells without asking", which nothing watched before
    # §11.2. 25% is Alpaca's standard equity maintenance requirement and
    # reproduces the spec's two published figures exactly: ~33% at 2.0x,
    # ~55% at 1.5x.
    maintenance_margin_pct: float = Field(default=25.0, gt=0, lt=100)
    # --- Stage 3 (shorts) -----------------------------------------------
    # Shorts carry the SAME limits as longs (owner decision 2026-09-17).
    # There is deliberately no short-specific concentration or gross-bearish
    # cap: the former `max_single_short_pct` (10) and `max_gross_bearish_pct`
    # (20) were unsourced numbers. One short is capped by `max_position_pct`
    # exactly as one long is (src/risk/rules.py), and the book either way is
    # bounded by `max_gross_exposure_x` and `max_total_position_pct`. Both
    # removed keys are rejected loudly by `_reject_removed_short_cap_keys`.
    # Sizing-only haircut (never applied to stop placement) on a short's
    # risk-per-share. A short gaps through its stop upward with no bound —
    # equal nominal risk is not equal real risk — so the same risk
    # allocation opens a SMALLER short than an equivalent long.
    #
    # 2026-09-26, board item 186: the DIRECTION above is arithmetic and needs
    # no citation. The MAGNITUDE 1.5 is still a chosen number. Researched and
    # deliberately NOT sourced: the skewness-pricing literature measures
    # expected returns to lottery-like stocks, not the size of an overnight
    # gap against a short, and the empirical overnight-gap studies are
    # index-level and disagree in sign. Measuring it properly needs a stored
    # daily-bar history this desk does not keep. The number ledger carries
    # the routed owner-appetite question; 1.5 means a short opens at
    # two-thirds the size of a long carrying the same stated risk.
    short_gap_risk_multiple: float = Field(
        default=SHORT_GAP_RISK_MULTIPLE_DEFAULT, gt=1.0, le=3.0,
    )
    # --- Kill switch (2026-09-02 operational safety guard) ---------------
    # A file whose mere EXISTENCE halts every order this desk would place —
    # entries, exits, covers, and protective-stop placement/replacement
    # alike. Read with `Path(...).exists()` and nothing else: no parsing, no
    # schema, so a malformed or empty file still halts — it cannot fail open
    # on bad content because it never reads any content. Ops stops the desk
    # with `touch <path>` and resumes it by deleting the file: no code
    # change, no deploy, and it takes effect on the NEXT order attempt even
    # if the process was already mid-session when the file appeared.
    #
    # Checked in `src/execution/broker.py` (the deterministic execution
    # layer), never by an agent or a prompt — a language model has no path
    # to talk the desk out of a halt it cannot see or reason about.
    #
    # UNLIKE every other guard in this file, this ONE also blocks
    # risk-REDUCING orders. Every other hard block and circuit breaker here
    # deliberately lets a SELL/COVER through even while it blocks new risk
    # (`RiskRuleEngine.check`'s `action in ("SELL", "COVER")` exemption
    # below), precisely so a bad account state can never trap a position.
    # The kill switch is the one lever that overrides that, for the case
    # where ops needs EVERYTHING stopped — including an exit that might
    # otherwise go out into a broken/stale market. It only blocks NEW
    # broker-bound order flow; a protective stop already resting at the
    # broker from before the halt is untouched and keeps protecting the
    # position.
    kill_switch_path: str = Field(default="data/KILL_SWITCH")

    #: Spec §10.3. Multiple of `max_sector_pct` used as the absolute sector
    #: ceiling when `max_sector_hard_pct` is not set explicitly. ClassVar, so
    #: pydantic treats it as a constant rather than a settable field.
    SECTOR_HARD_MULTIPLE: ClassVar[float] = 1.5

    #: Spec §12.3. The terminal bound on the DERIVED ceiling. With the target
    #: at 75 (§12.3) the 1.5x multiple gives 112.5, which is not a ceiling at
    #: all — a dial with no terminal bound bounds nothing. 90 keeps a real
    #: ceiling while leaving 15 points of scaling range above the target.
    #:
    #: NOT IN THE RATIFIED §12.3 TEXT: the spec set the target and left the
    #: terminal bound unstated. 90 was chosen when §12.3 was built and is open
    #: for the owner to move. `risk.max_sector_hard_pct` in settings.yaml sets
    #: it explicitly and overrides this derivation entirely.
    SECTOR_HARD_CEILING_MAX: ClassVar[float] = 90.0

    @property
    def sector_hard_ceiling_pct(self) -> float:
        """The absolute sector ceiling, explicit or derived.

        Every consumer reads this rather than `max_sector_hard_pct` directly,
        so the derivation rule lives in exactly one place.

        Derived = 1.5x the target, capped at `SECTOR_HARD_CEILING_MAX` (90),
        and never below the target itself — a ceiling under the target it
        backstops would make the scaling band run backwards.
        """
        if self.max_sector_hard_pct is not None:
            return self.max_sector_hard_pct
        derived = min(
            self.SECTOR_HARD_CEILING_MAX,
            self.max_sector_pct * self.SECTOR_HARD_MULTIPLE,
        )
        return min(100.0, max(self.max_sector_pct, derived))

    @model_validator(mode="after")
    def _sector_hard_ceiling_is_above_the_target(self):
        # A hard ceiling below the diversification target would mean the
        # scaling band runs backwards, and `sector_size_scale` would fall
        # back to gate behaviour silently. That is a config error worth
        # failing on rather than absorbing: the operator asked for something
        # incoherent and would otherwise never find out.
        if (
            self.max_sector_hard_pct is not None
            and self.max_sector_hard_pct < self.max_sector_pct
        ):
            raise ValueError(
                "risk.max_sector_hard_pct "
                f"({self.max_sector_hard_pct}) must be >= risk.max_sector_pct "
                f"({self.max_sector_pct}) — the absolute ceiling cannot sit "
                "below the diversification target it backstops"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_loss_alarm_keys(cls, data):
        # Owner instruction 2026-09-20 (docs/INCIDENT_HISTORY.md, retired
        # board item 32): the entire account-level loss-alarm mechanism — the
        # daily halt and the 5-day / 20-day BUY-halving brakes — was removed.
        # Same pattern and reason as the validators below: `extra="ignore"`
        # would let a stale deployment's settings.yaml keep these keys and
        # load silently, and an operator would believe a daily halt was
        # armed when nothing reads it. That is the single most dangerous
        # form this particular removal could rot into, because the belief
        # it creates is a belief about loss protection.
        if isinstance(data, dict):
            stale = [
                k for k in (
                    "max_daily_loss_pct",           # retired-ok
                    "daily_loss_risk_multiple",      # retired-ok
                    "drawdown_vol_sensitivity",      # retired-ok
                    "drawdown_5d_risk_multiple",     # retired-ok
                    "drawdown_20d_risk_multiple",    # retired-ok
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} was removed 2026-09-20 on "
                    "the owner's instruction: the account-level daily-loss "
                    "halt and the 5-day/20-day rolling-return BUY brakes are "
                    "retired in full (docs/INCIDENT_HISTORY.md, board item "
                    "32). There is NO replacement key and no account-level "
                    "loss limit — per-position stops are the desk's loss "
                    "protection, and the §11.2 gross-exposure de-levering "
                    "ladder (risk.max_gross_exposure_x) is the only "
                    "account-level drawdown response left. Delete the key "
                    "from the settings file."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_agreement_ceiling_key(cls, data):
        # Same pattern as `_reject_removed_short_cap_keys` below and
        # `ExecutionConfig._reject_deleted_repeg_keys`: BaseModel's default
        # `extra="ignore"` would let a stale deployment's settings.yaml keep
        # the key and load silently, and an operator would believe a sizing
        # ladder they set was in force when nothing reads it.
        if isinstance(data, dict) and "agreement_ceiling_pct" in data:
            raise ValueError(
                "risk.agreement_ceiling_pct was removed 2026-09-14: the "
                "graduated agreement sizing ladder is retired (the sqrt(n/5) "
                "law prices INDEPENDENT estimates and this desk's seats are "
                "not independent). Agreement is now a refusal gate only — "
                "see src/risk/rules.py::agreement_refuses_trade. Delete the "
                "key from the settings file."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_short_cap_keys(cls, data):
        # Owner decision 2026-09-17: shorts carry the same limits as longs.
        # `max_single_short_pct` and `max_gross_bearish_pct` (and the latter's
        # pre-2026-08-30 name `max_short_gross_pct`) no longer exist. Same
        # pattern and reason as the validators around it: `extra="ignore"`
        # would let a settings.yaml still carrying one load silently, and an
        # operator would believe a short cap was in force when nothing reads
        # it. There is no replacement key — `max_position_pct` now governs a
        # short exactly as it governs a long.
        if isinstance(data, dict):
            stale = [
                k for k in (
                    "max_single_short_pct",
                    "max_gross_bearish_pct",
                    "max_short_gross_pct",
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} removed 2026-09-17: shorts "
                    "carry the same limits as longs (risk.max_position_pct, "
                    "risk.max_gross_exposure_x, risk.max_total_position_pct). "
                    "Delete the key from the settings file; there is no "
                    "replacement key."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_level_match_key(cls, data):
        # docs/WORK.md item 46 (2026-09-13). Same pattern and same reason as
        # the validators above: `extra="ignore"` would let a settings.yaml
        # still carrying this key load silently, and an operator would
        # believe a match tolerance they set was in force when nothing reads
        # it any more. There is deliberately NO replacement key to point at
        # — the tolerance is no longer configurable, because it is derived
        # from the level zone's own definition. See the block where this
        # field used to be declared, above.
        if isinstance(data, dict) and "level_match_atr_tolerance" in data:
            raise ValueError(
                "risk.level_match_atr_tolerance was removed 2026-09-13 "
                "(docs/WORK.md item 46): an ATR multiple can never stay "
                "consistent with the percentage-of-price zone it claimed to "
                "cover. The tolerance is now derived from "
                "src.data.levels.CLUSTER_TOLERANCE_PCT and is not "
                "configurable. Delete the key from the settings file; there "
                "is no replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_reward_risk_floor_key(cls, data):
        # Board item 81 (2026-09-24). Same pattern and same reason as the
        # validators above: `extra="ignore"` would let a settings.yaml still
        # carrying this key load silently, and an operator would believe a
        # reward:risk floor was in force when nothing read it. It refused
        # nothing and capped nothing since 2026-09-17 (owner: residual
        # invented R/R is a defect) — see `src.risk.constants.REWARD_RISK_FLOOR`
        # for the full history. There is no replacement key.
        if isinstance(data, dict) and "min_reward_risk_after_widening" in data:
            raise ValueError(
                "risk.min_reward_risk_after_widening was removed 2026-09-24 "
                "(board item 81): it refused nothing and capped nothing. "
                "Delete the key from the settings file; there is no "
                "replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_stop_width_gate_key(cls, data):
        # Board item 56 (2026-09-26), route (c). Same pattern and same
        # reason as the validator above: with `extra="ignore"` a
        # settings.yaml still carrying this key would load silently and an
        # operator would believe a stop-width refusal was in force when
        # nothing reads it. The gate is deleted, not retuned -- see
        # docs/INCIDENT_HISTORY.md 2026-09-26. There is no replacement key:
        # width is answered by position size, and the touch-probability
        # READING is still recorded on every sized stop
        # (`src.data.levels.touch_probability`).
        if isinstance(data, dict) and "max_stop_width_reach_atr_multiple" in data:
            raise ValueError(
                "risk.max_stop_width_reach_atr_multiple was removed "
                "2026-09-26 (board item 56): the stop-width refusal it "
                "thresholded is deleted, never having refused a single "
                "trade. Delete the key from the settings file; there is no "
                "replacement key. `max_target_reach_atr_multiple` is a "
                "different number and is unchanged."
            )
        return data


class CashSweepConfig(BaseModel):
    """Idle-cash sweep into a T-bill ETF (default SGOV).

    The sweep vehicle is treated as CASH-EQUIVALENT everywhere: excluded
    from every LLM-facing position view, excluded from risk-engine exposure
    math (its market value counts toward cash in the cash_only filter),
    exempt from stop-coverage audits (it deliberately carries no stop), and
    force_delever liquidates it FIRST. Deterministic and zero-LLM — the
    LLM never decides to park or unpark; the pipeline bookends do.
    """
    enabled: bool = False
    """Master switch. False = the sweeper is inert everywhere (no view
    filtering, no funding sells, no parking buys)."""

    symbol: str = "SGOV"
    """The parking vehicle. Must be a cash-like T-bill ETF (SGOV/BIL);
    anything with real market beta breaks the cash-equivalence assumption
    that justifies every exemption listed above."""

    min_order_usd: float = Field(default=500.0, ge=0)
    """Don't churn sub-$500 parking orders — spread + noise beat the
    few cents of yield."""

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v:
            raise ValueError("cash_sweep.symbol must be a non-empty ticker")
        return v


class CashReserveConfig(BaseModel):
    """The raw-cash reserve band the /account liquidity view reports.

    RELOCATED 2026-10-01 (board item 190) out of `CashSweepConfig`, value
    unchanged. It was never part of the retired cash sweep's own machinery:
    `src.api.routes_live._compute_liquidity` reads it on every /account
    request to report `reserve_usd` and `cash_above_reserve`, and that
    reader outlives the sweep. Kept here so retiring the rest of the sweep
    cannot delete a live display band by association.
    """

    pct: float = Field(default=1.0, ge=0, le=20)
    """% of equity reported as held back as raw cash for fees, slippage and
    partial fills. Deliberately 1.0 — an earlier pass in the 2026-08-19
    tranche raised it to 5.0 as a workaround for BUYs being skipped for lack
    of cash, which treated a symptom and was put back. Still `arbitrary` in
    config/number_ledger.yaml; relocation changed its home, not its value or
    its honesty label."""


class EventRiskConfig(BaseModel):
    """Scheduled-event lookups that ground the Risk Manager's mandatory
    `event_risk` check (`src/data/event_calendar.py`).

    Added because that check was previously answered from the model's own
    memory: `MarketDataProvider.get_next_earnings_date` had zero callers, and
    no module fetched a macro release calendar at all. The numbers here are
    ceilings, not tuning knobs — a session must never be delayed, and must
    certainly never hang, because a nice-to-have calendar was slow. The FRED
    retry/backoff policy itself is NOT duplicated here: the calendar hits the
    same host as `src/data/macro.py` with the same failure mode, so
    `src/pipeline.py` threads the existing `macro.*` retry settings into it and
    only the deadline below is calendar-specific.
    """

    horizon_days: int = Field(default=10, ge=1, le=60)
    """How far ahead the macro release calendar looks, in calendar days. 10
    covers "the next few sessions" the `event_risk` field asks about with
    enough margin to see a release the desk should already be positioning
    around, without burying the seat in rows it will skim past."""

    calendar_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `get_upcoming_events()` call. Much
    tighter than `macro.total_fetch_deadline_s` (90s) on purpose: the macro
    summary is load-bearing for the regime call, this calendar is an
    advisory layered on top of a session that must not wait for it. Enforced
    the same way — every request timeout and every backoff sleep is clipped to
    the remaining budget, and releases not yet started are skipped and reported
    as `fetch_deadline_exceeded` rather than silently omitted."""

    earnings_deadline_s: float = Field(default=20.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for the whole per-symbol earnings-date sweep.
    Symbols not reached inside it come back labelled
    `unavailable_deadline_exceeded`, never dropped."""

    earnings_symbol_timeout_s: float = Field(default=8.0, ge=0.5, le=60.0)
    """Per-symbol ceiling on the earnings-date lookup. `yfinance`'s calendar
    call has no timeout of its own — the same hang risk `get_ohlcv` /
    `get_valuation_metrics` are already `ThreadPoolExecutor`-bounded against."""

    fomc_request_timeout_s: float = Field(default=10.0, ge=1.0, le=60.0)
    """Per-request timeout for the Federal Reserve's own FOMC calendar. Its own
    setting rather than a reuse of `macro.request_timeout_s` because this is a
    different host with a different failure mode — federalreserve.gov, not
    FRED. The backoff CURVE is still taken from `macro.*`: that is a generic
    retry policy, not a fact about either host."""

    fomc_max_retries: int = Field(default=2, ge=0, le=5)
    """Retries per Fed calendar URL before that source is given up on."""

    fomc_deadline_s: float = Field(default=15.0, ge=1.0, le=120.0)
    """Hard wall-clock ceiling for one `FOMCCalendarProvider.get_meetings()`
    call, covering BOTH the JSON feed and the fallback page. Same enforcement
    as the macro calendar: every request timeout and every backoff sleep is
    clipped to what remains, and a source not reached inside the budget is
    reported as a named absence rather than silently skipped."""

    fomc_cache_ttl_days: float = Field(default=7.0, ge=0.0, le=90.0)
    """How long a cached FOMC schedule is trusted without a refetch. FOMC dates
    are published a year ahead and change perhaps twice a year, so a weekly
    refresh is generous. Freshness alone is never sufficient: a cache is used
    without fetching only if it ALSO spans `horizon_days`, and an expired cache
    is still served — clearly labelled `measured_from_stale_cache`, with its
    age — when the live sources are unreachable."""

    fomc_cache_path: str = Field(default="data/fomc_calendar.json")
    """Where that cache lives. Relative by design, like the other on-disk
    caches (`data/company_profiles.json`, `data/news`, ...), so the rehearsal
    rig's chdir-based filesystem wall redirects it into the sandbox."""

    @model_validator(mode="after")
    def _deadlines_are_well_formed(self):
        if self.earnings_deadline_s < self.earnings_symbol_timeout_s:
            raise ValueError(
                "event_risk.earnings_deadline_s must be >= "
                "earnings_symbol_timeout_s — a sweep budget shorter than one "
                "symbol's own timeout would abandon every symbol before it "
                f"could answer; got {self.earnings_deadline_s} < "
                f"{self.earnings_symbol_timeout_s}"
            )
        if self.fomc_deadline_s < self.fomc_request_timeout_s:
            raise ValueError(
                "event_risk.fomc_deadline_s must be >= fomc_request_timeout_s "
                "— a deadline shorter than one request's own timeout would "
                "abort every fetch immediately without ever really trying; got "
                f"{self.fomc_deadline_s} < {self.fomc_request_timeout_s}"
            )
        return self
