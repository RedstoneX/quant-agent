"""Fail the build when a new trade-governing number arrives with no source.

THE DEFECT. The desk's hardest standing rule (`docs/OUTCOME.md`, and the top
of `docs/WORK.md`) is that a number which governs a trade must be read off the
instrument, derived from something that is itself read off the instrument, or
carry a written derivation at its definition site. Approval does not make a
flat number non-arbitrary, and neither does backtesting it into place. On
2026-09-11 an audit listed about twenty such numbers; on 2026-09-14 every one
was re-checked and every one was still live; the finding was filed as "an
inventory, not an item / never re-audit" and nothing was assigned. A week
later they were all still live and the count had grown.

That is the real defect. Not the list — the absence of any boundary. **There
was no mechanical check of any kind**, so the next invented number got in for
free.

WHAT THIS MODULE DOES. It enumerates every numeric DEFINITION SITE inside a
declared scope, and requires each one to carry an entry in a checked-in
ledger (`config/number_ledger.yaml`) saying where the number came from. A new
number in scope with no ledger entry fails `pytest`, which is the check
branch protection requires. The ledger is the inventory, and because the
build reads it, it is an inventory that cannot rot unnoticed.

WHAT IT DOES NOT DO, SAID FIRST BECAUSE IT IS THE HONEST FRAMING. This gate
tests that a justification EXISTS, in a shape a reader can open. It does not
and cannot test that the justification is TRUE. That distinction is not
academic: the entry this module was built around was written on shipping day,
was the most scrutinised line in the ledger, and was **false in four places**
— it claimed `min_position_risk_pct`'s 0.50% was absent from `settings.yaml`
(it is at `config/settings.yaml:751`), absent from every document (it is the
owner-ratified envelope row at `docs/OUTCOME.md:84`), absent from any
ratification record (`75c02335`, 2026-08-27), and existing in one place while
the ledger itself listed it twice. Every one of those was a one-minute grep.
So the rules below are built to make a claim CHEAP TO FALSIFY rather than to
adjudicate it: `source` must be a URL or a `file:line` a reader can open, and
`arbitrary` must carry the open question and its cost rather than being the
quiet default.

WHY A REGISTRY AND NOT AN ANNOTATION. A required comment marker (`# source:`)
was the cheaper design and was rejected: the failure this closes is that
nobody remembers the rule, and a marker is only present if somebody
remembered to type it — the check would have to accept its absence as "not a
trade number" and would therefore catch nothing. The ledger inverts that. The
scanner decides what is in scope from the code's own structure; the author
cannot opt out by staying quiet, only by writing an entry somebody reviews.

SCOPE, AND THE RULE BEHIND IT. Scope was a hand-kept file list with no stated
admission rule, which meant nothing distinguished a module that belongs from
one that does not — `src/execution/cash_sweep.py` was in and
`src/data/technical.py`, which defines the ATR period every stop multiple in
the ledger is a multiple of, was out. The rule is now written down:

  IN SCOPE: every module on the path from a seat's verdict to a broker order
  — the risk engine, the constructor, ranking, nomination, the rotation and
  cash decisions, the execution modules that price or gate an order, and the
  INDICATOR AND LEVEL modules whose outputs stops and sizes are computed
  from. A unit is in scope wherever its multiplier is.

  OUT OF SCOPE: fetching, caching, persistence, reporting, notification and
  LLM plumbing. A wrong number there degrades data or messaging, which the
  seats already see as missing evidence, and which is a different failure.

That rule is still prose, so it is backed mechanically by
`MAX_UNSCOPED_NUMERIC_SITES`: the same scanner is run over every `src/**.py`
NOT in scope, and the build fails if that count RISES. A new module-level
numeric constant in an unscoped file therefore cannot arrive silently — it
either comes into scope with a ledger entry, or the ceiling is raised as a
reviewed one-line edit that says so. (This is also the answer to
`stop_repair.py`: it defines no module-level numeric constant at all, so
there is nothing there for either check to see.)

Inside a scoped file the structural rules are:
  * (a) module-level UPPER_CASE names bound to a number;
  * (b) numeric defaults on fields of a class whose name ends in `Config`;
  * (c) numeric defaults on function and method PARAMETERS
    (`max_pct: float = 5.0`), id `module.Qual.name(param)`;
  * (d) numeric attributes on ANY class, annotated or not
    (`STOP_LIMIT_BUFFER_PCT = 0.03` inside the broker class), id
    `module.Qual.attr`; and
  * (e) inline MULTIPLIER/DIVISOR literals in the band `FACTOR_BAND`
    ([0.5, 2.0), excluding +-1): `round(price * 0.995, 2)`,
    `deficit * 1.02`, `price * (1.01 if cover else 0.99)`. Id
    `module.Qual.function:factor[N]`, N counting such literals in that
    function in source order.

Rules (c)-(e) were added 2026-09-19 because (a)/(b) left live trade numbers
invisible: the queued-earnings weight cap and the correlated-cluster cap were
parameter defaults, the 3% stop-limit buffer was a class attribute, and board
item 138's 0.5%/1% order-price buffers were inline arithmetic. (c)-(e) apply
only to scoped modules, not to `src/config.py`'s named classes and not to the
unscoped sentinel, whose count stays defined as module-level constants.

Why (e) is a band and not "every literal". Every literal operand of
arithmetic in scope was listed on 2026-09-19: about seventy, and outside the
band they are unit conversions (10_000 bps, 365 days, 60 s, 1_000_000), float
epsilons and query paddings; inside it, every one was an order-price or
sizing margin. A literal whose other operand is also a literal (`365 * 5`) is
constant arithmetic, not a margin, and is skipped. Renumbering is a feature:
adding a factor above an existing one in the same function shifts N and
fails the gate, which puts the neighbouring entries back in front of a
reviewer.

Rule (b) predates (d) and is kept because `*Config` is this codebase's
settled name for "the tunables". Rule (d) widens it to every class; it did
not bury the signal, because result/DTO dataclasses (`GrossCeilingOutcome`,
`PortfolioVolEstimate`, `SizingDecision`) default their numbers to zero,
which is never a site. Measured 2026-09-19: rule (d) found 19 sites in
scope, one of them a DTO counter (`AgentResult.provider_requests = 1`).

"Bound to a number" means bound to a number however it is spelled. A default
written as a NAME (`min_position_risk_pct: float = STARTER_POSITION_RISK_PCT`)
or as constant arithmetic (`5 * 366`) used to return None from `_numeric` and
vanish — four in-scope fields were in exactly that state, and pointing such a
name at an unscoped module was a one-line way to make any number disappear.
Names are now resolved against the module's own constants and against the
repo modules it imports them from, and constant arithmetic is folded.

Numeric leaves inside tuple, list and dict literals are sites too. That is
not completeness for its own sake: `stop_atr_setup_scale` holds the
stop-width scalers as a tuple of pairs, and those multiply into every stop
distance.

SEVEN THINGS THE LEDGER IS CHECKED FOR:

  1. COVERAGE — every in-scope site has an entry. A new number fails.
  2. VALUE — the entry's recorded value equals the live literal, AND, where
     the field is reachable from `config/settings.yaml`, equals the DEPLOYED
     value there too. Without the second half the ledger pinned the code
     default while the desk traded the YAML: `risk.max_position_risk_pct`
     could go 5 -> 10 with this gate silent. 52 of the entries route that
     way (verified by resolving each `AppConfig` section to its class).
  3. GROUNDS — `sourced`/`instrument` require a `source` that is a URL or a
     `file:line`, because prose is not falsifiable by a non-author;
     `derived` requires naming its base; `arbitrary` requires a note, the
     open question in answerable form, and what it costs while unanswered.
  4. BASE DRIFT — a `derived` entry records `base_value`, the value its base
     held when the derivation was written. If the base later moves, the two
     disagree and the build fails. This is the *sourced once, unsourced
     later* class.
  5. RATCHET — the `arbitrary` count must EQUAL `MAX_ARBITRARY_ENTRIES`, not
     merely stay under it. A ceiling was gameable: move a trade constant into
     an unscoped file, delete its ledger row, and the build went green while
     the headline arbitrary count FELL and the number became less visible
     than before the gate existed. Equality means a row can only leave the
     ledger alongside a declared edit to the count.
  6. UNSCOPED SENTINEL — `MAX_UNSCOPED_NUMERIC_SITES`, above.
  7. CITATIONS RESOLVE — every `path:line` an entry cites must exist and
     the line must be inside the file. It cannot check that a citation
     SAYS what the entry claims, but it catches one nobody opened. It
     caught an invented document path during this gate's own rework.

WHAT THIS STRUCTURALLY CANNOT CATCH, stated here so the module is never cited
as if it covered more:

  * A WRONG source. This is the big one and it has already happened; see the
    second paragraph. Nothing here reads a citation and checks it is true.
  * A number outside scope that the sentinel's count ratchet lets through
    because something else in an unscoped file was deleted in the same
    commit.
  * A `path:line` citation that has DRIFTED. Rule 7 catches a path that does
    not exist and a line past the end of a file; it cannot tell that line 930
    of a file that is still 2000 lines long stopped being the line meant. Three
    of this ledger's own citations drifted by 20-30 lines inside one day of
    merges and were re-checked by hand. Prefer a URL where one exists.
  * A number computed at run time from live inputs, or a `default_factory`
    whose number lives in a function body.
  * Inline literals OUTSIDE rule (e)'s band or shape. Measured 2026-09-19:
    comparison thresholds (`abs(volume_change_pct) > 50`), additive offsets,
    divisors like the `/ 10.0` in `src/data/levels.py`'s level strength
    (board item 148), fallback arguments (`_risk_number(x, 25.0)`,
    `kwargs.get(k, 25.0)`), and keyword literals passed at a call site
    (`lookback_days=5`) are all invisible. Catching them by value needs a
    list of "boring" numbers, which is itself an arbitrary list; the honest
    fix per site is to hoist the literal to a named constant, which (a)
    then sees.
  * A number in prompt PROSE. Measured 2026-09-18: ~1,825 numeric tokens
    across `config/prompts/*.md`, overwhelmingly dates and list numbering.
    That is board item 105 and it is not solvable this way.
  * The `arbitrary` entries themselves. The gate holds the boundary; it does
    not retroactively source anything.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Repository root, resolved from this file rather than the cwd so the check
#: behaves the same under pytest, a git hook and a direct run.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The ledger. Data, not code: it holds no value the desk trades on, only a
#: record of where each traded value is supposed to have come from.
LEDGER_PATH = REPO_ROOT / "config" / "number_ledger.yaml"

#: Deployed overrides. Every `src.config.*Config.<field>` ledger row is also
#: checked against this file, because this is the value the desk trades.
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

#: The modules on the path from a verdict to a broker order. See the SCOPE
#: rule in the module docstring; this list is the rule applied, and
#: `MAX_UNSCOPED_NUMERIC_SITES` is what stops it from silently lagging.
#: A directory entry covers every `.py` under it.
SCOPED_PATHS: tuple[str, ...] = (
    "src/risk",
    "src/portfolio_constructor.py",
    "src/rotation.py",
    "src/nominations.py",
    "src/evidence_gate.py",
    "src/verdicts.py",
    "src/data/correlation.py",
    # 2026-09-19, board item 124: the research-defined insider purchase
    # cluster now lifts the smart-money seat's conviction, so its definition
    # is on the path from a verdict to an order.
    "src/data/smart_money_cluster.py",
    # The indicator and level units. Every ATR multiple and every stop the
    # ledger tracks is a multiple of `technical.ATR_PERIOD`, and levels are
    # where stops are placed; watching the multiplier and not the unit was
    # the gap the scope rule above was written to close.
    "src/data/technical.py",
    "src/data/levels.py",
    # Decides whether an APPROVED trade is actually sent
    # (`MAX_ENTRY_SLIPPAGE_BPS`), and carries the sizing fallback.
    "src/pipeline_stages.py",
    "src/execution/cash_sweep.py",
    "src/execution/stop_records.py",
    # 2026-09-19, board item 130: `broker.py` IS the broker order -- the
    # scope rule's own words ("every module on the path from a seat's
    # verdict to a broker order") named this file and it was not here.
    # `stop_repair.py` and `coverage_watchdog.py` are the repair/alarm path
    # for a protective stop that failed to place. `stop_repair.py` still
    # defines no module-level numeric constant (see the docstring note this
    # entry used to require); scoping it adds nothing today but stops a
    # future one arriving unseen.
    "src/execution/broker.py",
    "src/execution/stop_repair.py",
    "src/coverage_watchdog.py",
    # The pipeline's own decision/execution glue. `_clamp_queued_earnings_
    # buys`' `max_pct=5.0` is a function-parameter default and the de-lever
    # and midday order-price buffers are inline multipliers; rules (c) and
    # (e) see them since 2026-09-19.
    "src/pipeline.py",
    # Every seat's prompt-construction and LLM-call code -- the path from
    # evidence to a seat's verdict the scope rule names. Most of what lives
    # here is LLM plumbing (timeouts, retries, token budgets) that is
    # `not-trade-governing` once seen; the truncation caps and rank tables
    # that shape what evidence a verdict is built from are not.
    "src/agents",
    # 2026-09-19: the universe admission screen. Every threshold that decides
    # whether a symbol may be traded at all lives here or in
    # `UniverseScreenConfig`.
    "src/universe_screen.py",
)

#: Config classes inside scoped files whose numeric field defaults are sites.
#: Matched by SUFFIX, so a new `FooConfig` is covered the day it is written.
CONFIG_CLASS_SUFFIX = "Config"

#: `src/config.py` holds every seat's settings in one file, most of them
#: nothing to do with a trade (LLM cost circuits, Telegram retries, evolution
#: bookkeeping). Scoping the whole file would bury the signal, so the
#: trade-governing classes are named. Same suffix rule applies inside them.
SCOPED_CONFIG_CLASSES: tuple[str, ...] = (
    "RiskConfig",
    "ExecutionConfig",
    "CashSweepConfig",
    "IntradayScanConfig",
    "SmartMoneyConfig",
    "NominationConfig",
    "EventRiskConfig",
    "UniverseScreenConfig",
)
SCOPED_CONFIG_MODULE = "src/config.py"

#: Zero alone is excluded as a definition site: it is the empty/neutral
#: default on a result field and the bottom of an ordinal scale, and no
#: reviewer has anything to say about it.
#:
#: 1.0 USED TO BE EXCLUDED HERE, justified as "the identity, not a setting"
#: with the claim that every audited number sat outside the set. That claim
#: was false and the exclusion hid real settings:
#: `absolute_min_stop_atr_multiple = 1.0` (in both `RiskConfig` and
#: `ConstructorConfig`), `min_target_atr_multiple = 1.0`,
#: `breakout_projection_atr_multiple = 1.0`, `NOISE_BAND_ATR_MULTIPLE = 1.0`
#: and `CashSweepConfig.reserve_pct = 1.0`. One ATR is not an identity — it
#: is the hard floor under every stop this desk sets.
NEUTRAL_VALUES: frozenset[float] = frozenset({0.0})

#: Classifications a ledger entry may carry.
#:   instrument       — read off the instrument at run time or fixed by an
#:                      external spec the desk does not choose (a broker tick
#:                      size, an exchange rule, a statute). Requires a
#:                      falsifiable `source`.
#:   sourced          — a cited derivation or published source written down.
#:                      Requires a falsifiable `source`.
#:   derived          — computed from another ledger entry. Requires
#:                      `derived_from` and `base_value`.
#:   arbitrary        — live, governs trades, and nothing backs it. This is a
#:                      DEBT INSTRUMENT, not a status: it requires `note`,
#:                      `open_question` and `cost_while_unanswered`, and it is
#:                      ratcheted. It must never be the cheapest field to
#:                      write, which is what it was.
#:   not-trade-governing — in scope structurally, does not reach a trade
#:                      decision. Requires `note` saying why.
VALID_STATUSES: frozenset[str] = frozenset(
    {"instrument", "sourced", "derived", "arbitrary", "not-trade-governing"}
)

#: Fields every `arbitrary` entry must carry. `docs/OUTCOME.md`'s outcome-3
#: clause already demands all of this; the schema used to require none of it,
#: which made the honest-but-unsourced status the cheapest one to write and
#: put the incentive exactly backwards.
ARBITRARY_REQUIRED_FIELDS: tuple[str, ...] = (
    "note",
    "open_question",
    "cost_while_unanswered",
)

#: Ratchet, checked for EQUALITY. See rule 5 in the module docstring: as a
#: ceiling this was gameable by deleting a row. LOWER THIS when one is
#: sourced, in the same commit that sources it. Raising it is an owner
#: decision, not a build fix.
#: 2026-09-18: 86 -> 87. NOT a new number and not a loosened gate --
#: `max_filings_per_refresh` was misclassified `not-trade-governing` on the
#: claim that a fetch cap "never decides whether or how much to trade", and
#: that day the cap binding is what refused a trading decision. Correcting a
#: misclassification upward is the ratchet working; the debt was always
#: there, unrecorded.
#: 2026-09-18: 87 -> 88, the SAME correction to the SAME sentence one row
#: over. `refresh_deadline_s` carried that identical claim, and the same day
#: it was the deadline the intraday research-freshness check ran out of
#: while deciding whether the tick could decide at all (measured on the live
#: desk: the check went 39.6s -> 180.3s and hit this deadline on three
#: ticks). Again no number was added and none was loosened -- an unrecorded
#: debt was written down. That the same falsified sentence sat on two rows
#: is itself the finding: it is boilerplate, and boilerplate is not a
#: classification.
#: 2026-09-19: 88 -> 106, board item 130. Scoping `src/execution/broker.py`,
#: `src/coverage_watchdog.py`, `src/pipeline.py` and `src/agents` admitted
#: 47 new structural sites (0 in `src/execution/stop_repair.py`, which still
#: defines no module-level numeric constant); 18 are `arbitrary`: the
#: stop-placement retry ceiling and backoff pair item 129 already found
#: unjustified (3 sites), the smart-money role/freshness/signal-class rank
#: tables that order which insider findings reach synthesis (8 sites --
#: ordering DIRECTION has a research citation, the point values do not),
#: six smart-money prompt-truncation caps that drop findings/text/
#: transactions with no downstream flag that anything was dropped (the same
#: shape as the `max_filings_per_refresh` correction two entries above, so
#: classified the cautious way up front rather than after a counter-
#: example), and the technical seat's bars-per-symbol window (1 site). The
#: other 29 are `not-trade-governing`: reconnect/timeout/retry/batch-size
#: plumbing on a fill-notification socket or an LLM call, and two
#: floating-point epsilons guarding representation error rather than
#: choosing a policy value. No value was changed by this pass.
#: 2026-09-19: 106 -> 146. The scanner learned rules (c), (d) and (e)
#: (parameter defaults, attributes on any class, near-one inline
#: multipliers). 98 sites that were always live and never visible now carry
#: entries: 40 arbitrary, 29 derived, 29 not-trade-governing. The 40: fifteen
#: JSON-fragment anchor weights that choose which part of a seat's reply is
#: parsed (a stale table emptied a morning's trades on 2026-08-17/20);
#: fourteen feedback-memory windows and caps that decide what a trading seat
#: is shown; board item 138's buffers (3% stop-limit, 1% de-lever, 0.5%
#: exit, the 2% ask/bid skip multiple and the 2% sweep-sale cushion); the
#: queued-earnings 5% weight cap; the 50% correlated-cluster advisory; the
#: 4-day trail cooldown; the 50%-of-price stop sanity floor; the 0.5 ATR
#: research pre-filter; and the 5% preview size. No number was added and
#: none changed -- the debt was always there, unrecorded.
#: 2026-09-23: 142 -> 143, board item 180.
#: `src.data.technical.LONGEST_INDICATOR_WINDOW` was `sourced` on the
#: 200-day moving average being a standard published trend reference. That
#: sources the MA WINDOW; the SAME constant is also the bar count under
#: which `_require_sufficient_history` refuses a trade outright, and no
#: citation anywhere backs 200 as a data-sufficiency test. One status per
#: site, so the row takes the weaker use's status and the split is written
#: into its note -- the same correction shape as 146 -> 147. No value
#: changed and no number was added; an unrecorded debt was written down.
MAX_ARBITRARY_ENTRIES = 143  # 2026-09-25, board item 80 (reworked): +1 for `src.portfolio_constructor.ConstructorConfig.structural_stop_buffer_pct` (0.005), the owner-appetite buffer a no-ATR protective stop sits past the structural level it is read from (`_derive_structural_stop_no_atr`); nothing measured fixes a buffer size, so it is arbitrary by convention, ratified as appetite with an open question. Was 142  # 2026-09-25, board item 148: +1 for `src.data.levels.LEVEL_STRENGTH_DISTANCE_DIVISOR_PCT` (10.0), named out of an inline `/ 10.0` in the level-strength formula that rule (e)'s factor band could not see (this module's own docstring names the exact literal). No value or behaviour changed. The board item's other named literal, a flat 40% max-distance cap, no longer exists in the code -- it was replaced 2026-09-12 by the ATR-based `horizon_reach` window, which is already ledgered via `MAX_REACH_ATR_MULTIPLE`/`MAX_HORIZON_SESSIONS`; nothing new to track there. Was 141  # 2026-09-25, appetite-ratification campaign: -1 for `src.config.RiskConfig.min_level_touches_for_stop_honor`, reclassified `arbitrary`->`sourced` (in-repo measured real-vs-shuffled bounce-probability table, non-overlapping 95% CIs only at 5+ touches; the owner-ratified-appetite numbers in the same campaign stay `arbitrary` per convention and do NOT move the count). Was 142  # 2026-09-25, ledger cleanup: -1 for `src.risk.trailing.CHANDELIER_ATR_MULTIPLE`, reclassified `arbitrary`->`sourced` (published Chandelier Exit default multiple 3.0; only the multiple, not the desk's lookback/ATR geometry). Was 143  # 2026-09-25, item 142: +2 for `RANGE_SECOND_RATCHET_TRIGGER_R` and `RANGE_SECOND_RATCHET_LOCK_R`, both reclassified `derived`->`arbitrary` (chosen appetite multiples, not derivations of the breakeven R-unit). Was 141  # 2026-09-24, board item 81: -1 for `src.portfolio_constructor.ConstructorConfig.min_reward_risk_after_widening`, deleted as zero-reader dead code (nothing in `PortfolioConstructor` ever read it). Was 142  # 2026-09-24, item 118: -1 for `src.pipeline.TradingPipeline._force_delever:factor[1]`, re-sourced from `arbitrary` to `derived` — the forced de-lever's must-fill SELL limit now points at `AlpacaBroker.STOP_LIMIT_BUFFER_PCT` (the 3%-through buffer), matching the gross-ceiling de-lever. Was 143  # 2026-09-23, board item 180: +1 for `LONGEST_INDICATOR_WINDOW`, relabelled `arbitrary` because its refusal-gate use is unsourced (see the comment above). 2026-09-20, retired board item 32: -6 for the account-level loss alarms removed on the owner's direct instruction -- `RiskConfig.drawdown_20d_risk_multiple`, `RiskConfig.drawdown_5d_risk_multiple`, `DEFAULT_DRAWDOWN_VOL_SENSITIVITY`, `DRAWDOWN_BUY_SCALE`, `MIN_REALIZED_VOL_RETURNS` and `REALIZED_VOL_WINDOW_SESSIONS` (the volatility yardstick they were measured against went with them). Was 148.

#: Sentinel for the scope rule. Module-level numeric constants found by this
#: same scanner in `src/**.py` files that are NOT in scope. Measured, not
#: chosen. The build fails if it RISES, so a trade number cannot be parked
#: outside scope silently. Raising it is a reviewed line that says a new
#: unscoped constant was looked at and is not trade-governing.
MAX_UNSCOPED_NUMERIC_SITES = 153  # 2026-09-24, item 163: +1 for `src.models.RISK_NARRATIVE_MISMATCH_TOLERANCE_PCT` (0.5), the tolerance the new PM risk-narrative-mismatch check uses to compare an explicit risk-% claim in `TargetPosition.thesis` prose against the authoritative `risk_allocation_pct` field. Not an independent number -- it is `RiskConfig.min_position_risk_pct` (config/settings.yaml:659, already ledgered) duplicated as a literal because `TargetPosition` is an LLM-output model with no `RiskConfig` in scope at validation time. It only sets a durable, surfaced FLAG when prose and field disagree; `risk_allocation_pct` remains authoritative for sizing and is never overridden, so this cannot decide, size, price or exit a trade. Was 152  # 2026-09-24: +1 for `src.margin_interest.MAX_LOOKBACK_MONTHS` (6), the owner's own ask for how many months back the cumulative margin-interest view looks (this-week/current-month/up-to-6-months/all-time, replacing the old per-day/per-year cockpit and Telegram figures). It bounds how far back a PRESENTATION bucket looks, not any trade decision -- `overnight_debit_balance`/`estimate_daily_interest`, the actual interest math, are unchanged. Was 151  # 2026-09-23: +1 for `src.margin_interest.MAX_CALENDAR_LOOKAHEAD_DAYS` (7), the safety bound on the forward calendar walk that counts how many calendar days of margin interest the owner-facing ESTIMATE line will show (a Friday debit is carried 3 days). It bounds a Telegram/dashboard estimate and degrades to 1 when exhausted; it never decides, sizes, prices or exits a trade. Was 150  # 2026-09-23: +1 for `src.data.event_calendar.RELEASE_SCHEDULE_LOOKAHEAD_DAYS` (120), the width of the single `/fred/release/dates` request per configured release. It is a FETCH window, not a horizon: `get_upcoming_events` still filters to `horizon_days` before anything reaches a seat, so this number cannot decide, size, price or exit a trade -- it only decides whether the desk can SEE a monthly release's published schedule at all. At the previous 10-day width three of four major releases came back empty and were mislabelled as source failures (measured against the live FRED API 2026-09-23; the measurement is recorded at the constant). Was 149  # 2026-09-23: +2 for the new `src/llm_route_journal.py` (the SQLite connect timeout and the `read_events` default page size). That module is a durable log of which LLM road answered a call and what that road lists at; neither number decides, sizes, prices or exits a trade. The four numbers the same change added to `src/agents/base.py` are absent from this count because that module is in SCOPED_PATHS and each one carries a config/number_ledger.yaml entry. Was 147  # 2026-09-19: +2, and they are this module's own `FACTOR_BAND` (0.5, 2.0) -- the classifier band rule (e) uses to tell a price/size margin from a unit conversion. It governs what the gate sees, not any trade. Was 145  # 2026-09-19, board item 130: -47. `src/execution/broker.py`, `src/coverage_watchdog.py`, `src/pipeline.py` and `src/agents` moved from unscoped to SCOPED_PATHS (192 -> 145) and every one of their 47 structural sites now carries a ledger entry instead of sitting in this count; none was deleted or reclassified to make the number fall. Was 192  # 2026-09-18: +3 for the trade_updates reconnect ceilings in `src/execution/broker.py` — `_STREAM_ATTEMPT_CEILING_PER_SESSION` (6, the attempt at which alpaca-py's own 1s/30s equal-jitter curve saturates), `_STREAM_ATTEMPT_CEILING_PER_DAY` (200, one minute of Alpaca's published 200-requests-per-minute account allowance, cross-checked against the measured 56 and 50 attempts of 2026-09-16/17) and `_STREAM_RATE_LIMIT_STAND_DOWN_S` (60, the published rate-limit window a 429 must sit out). They bound a fill-NOTIFICATION socket's retry loop after it logged 32,896 handshakes and 32,666 HTTP 429s on 2026-09-15; none of them decides, sizes, prices or exits a trade — the bounded REST fill path is unchanged and is what runs when they fire.  # was 189 (+1 for `src/trader_feed.py::_COMPANY_NAME_CAP`, presentation only).

#: Paths under `src/` the unscoped sentinel does not count: generated code and
#: vendored trees have no author to ask.
UNSCOPED_SENTINEL_EXCLUDE: tuple[str, ...] = ("src/frontend",)


@dataclass(frozen=True)
class NumberSite:
    """One numeric definition site the ledger must account for."""

    site_id: str
    path: str
    lineno: int
    value: float

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.site_id} = {self.value!r}  ({self.path}:{self.lineno})"


@dataclass
class LedgerProblem:
    """One reason the build should fail, in the words a reviewer needs."""

    kind: str
    site_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"[{self.kind}] {self.site_id}: {self.detail}"


def _numeric(node: ast.AST, names: dict[str, float] | None = None) -> float | None:
    """The literal value of `node`, or None if it is not a number.

    Handles the unary minus that `ast` represents as an operator rather than
    as part of the constant, so `-20.0` is one site and not a miss.

    `names` maps module-level constant names to their values (the module's own
    plus the ones it imports from other repo modules). Without it, a default
    bound to a NAME rather than to a literal returns None and the number
    vanishes from the gate entirely — which was a one-line way to hide any
    number by pointing a scoped field at an unscoped module. Constant
    arithmetic (`5 * 366`) is folded for the same reason.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand, names)
        return None if inner is None else -inner
    if names and isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _numeric(node.left, names)
        right = _numeric(node.right, names)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return float(left**right)
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
    return None


def _module_constants(tree: ast.Module) -> dict[str, float]:
    """Module-level names bound to a plain number, in definition order.

    Deliberately shallow: a name bound to a call, a container or a comprehension
    is not resolved, because the point is to follow the one-hop indirection an
    author reaches for, not to evaluate the module.
    """
    out: dict[str, float] = {}
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        value = _numeric(node.value, out)
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _imported_constants(tree: ast.Module, root: Path) -> dict[str, float]:
    """Numeric constants this module imports by name from other repo modules.

    `from src.risk.constants import STARTER_POSITION_RISK_PCT` makes that
    number the live default of a scoped field, so the gate has to see it
    whether or not `src/risk/constants.py` is itself in scope. That is the
    whole evasion: point the name somewhere unscoped and the number is gone.
    """
    out: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("src."):
            continue
        source = root / (node.module.replace(".", "/") + ".py")
        if not source.is_file():
            continue
        try:
            constants = _module_constants(ast.parse(source.read_text(encoding="utf-8")))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for alias in node.names:
            if alias.name in constants:
                out[alias.asname or alias.name] = constants[alias.name]
    return out


def _leaves(
    node: ast.AST,
    prefix: str,
    names: dict[str, float] | None = None,
    local: dict[str, float] | None = None,
) -> list[tuple[str, float, int]]:
    """Every numeric leaf under `node`, with a stable path-qualified id.

    A bare literal yields one leaf. A tuple, list or dict literal yields one
    leaf per numeric element, keyed by index or by its literal key, so
    `stop_atr_setup_scale`'s `("range", 0.90)` is addressable as
    `...stop_atr_setup_scale[1][1]` and moves only if the structure moves.

    `local` names the constants defined in this same file. A leaf that is
    just one of those names is NOT a second site — it is one number with two
    names, and ledgering it twice is how the flagship entry came to say a
    number existed in one place while the ledger listed it in two. A name
    bound to a number from ANOTHER module is still a site, because that is
    where the number enters this file.
    """
    if local and isinstance(node, ast.Name) and node.id in local:
        return []
    value = _numeric(node, names)
    if value is not None:
        return [(prefix, value, getattr(node, "lineno", 0))]

    out: list[tuple[str, float, int]] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for index, element in enumerate(node.elts):
            out.extend(_leaves(element, f"{prefix}[{index}]", names, local))
    elif isinstance(node, ast.Dict):
        for key, element in zip(node.keys, node.values):
            if isinstance(key, ast.Constant):
                label = f"[{key.value!r}]"
            else:
                label = "[?]"
            out.extend(_leaves(element, f"{prefix}{label}", names, local))
    return out


def _field_default(node: ast.AnnAssign) -> ast.AST | None:
    """The default expression of an annotated class field, or None.

    Covers a bare default (`x: float = 2.5`), a dataclass `field(default=...)`
    and a pydantic `Field(2.5, ...)` / `Field(default=2.5)`. A
    `default_factory` is deliberately NOT followed: the number then lives in
    a function body, which is out of scope and honestly declared as such.
    """
    if node.value is None:
        return None
    if isinstance(node.value, ast.Call):
        for keyword in node.value.keywords:
            if keyword.arg == "default":
                return keyword.value
        if node.value.args:
            return node.value.args[0]
        return None
    return node.value


def _scan_module(
    path: Path,
    rel: str,
    config_classes: tuple[str, ...] | None,
    root: Path | None = None,
) -> list[NumberSite]:
    """Sites in one file.

    `config_classes` None means "any class whose name ends in `Config`" — the
    rule for a scoped module. A tuple means only those names, which is how
    `src/config.py` is handled without pulling in the LLM and Telegram
    settings that share the file.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = rel[: -len(".py")].replace("/", ".")
    sites: list[NumberSite] = []

    local = _module_constants(tree)
    names = dict(local)
    if root is not None:
        # Imported names do not shadow the module's own.
        for key, value in _imported_constants(tree, root).items():
            names.setdefault(key, value)

    # (a) module-level UPPER_CASE constants.
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not (name.isupper() or (name.startswith("_") and name.lstrip("_").isupper())):
                continue
            for site_id, value, lineno in _leaves(
                node.value, f"{module}.{name}", names, local
            ):
                if value not in NEUTRAL_VALUES:
                    sites.append(NumberSite(site_id, rel, lineno, value))

    # (b) numeric defaults on `*Config` class fields.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if config_classes is None:
            if not node.name.endswith(CONFIG_CLASS_SUFFIX):
                continue
        elif node.name not in config_classes:
            continue
        for body_node in node.body:
            if not isinstance(body_node, ast.AnnAssign):
                continue
            if not isinstance(body_node.target, ast.Name):
                continue
            default = _field_default(body_node)
            if default is None:
                continue
            base = f"{module}.{node.name}.{body_node.target.id}"
            for site_id, value, lineno in _leaves(default, base, names, local):
                if value not in NEUTRAL_VALUES:
                    sites.append(
                        NumberSite(site_id, rel, lineno or body_node.lineno, value)
                    )

    if config_classes is None:
        sites.extend(_scan_extended_shapes(tree, module, rel, names, local))

    return sites


#: Rule (e)'s band. A literal in [0.5, 2.0), other than 1.0, used as a
#: multiplier or divisor is a price or size scaled by a policy margin — a
#: limit 0.5% through the market, a 2% proceeds cushion, a 1.25 ATR noise
#: floor. The band is a CLASSIFIER, not a trade number: it was chosen by
#: listing every literal operand of arithmetic in scope on 2026-09-19 (about
#: seventy) and finding that everything outside it is a unit conversion
#: (10_000 bps, 365 days, 60 s, 1_000_000), a float epsilon, or a query
#: padding, while everything inside it is an order-price or sizing margin.
#: 2.0 itself is excluded because halving/doubling is overwhelmingly an
#: identity of the arithmetic (a midpoint) rather than a chosen margin.
FACTOR_BAND: tuple[float, float] = (0.5, 2.0)


def _qualified_scopes(tree: ast.Module):
    """Yield `(node, qualname)` for every function and class in the module.

    `qualname` is the dotted chain of enclosing class and function names,
    the same path a reader would use to find the definition.
    """

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                yield child, qual
                yield from visit(child, qual)
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _factor_operands(node: ast.AST) -> list[ast.AST]:
    """The operand itself, or both branches of `(a if cond else b)`."""
    if isinstance(node, ast.IfExp):
        return _factor_operands(node.body) + _factor_operands(node.orelse)
    return [node]


def _own_nodes(func: ast.AST):
    """Every node in `func`'s body that is not inside a nested def or class.

    A nested function's literals belong to that function's own qualname, so
    an edit inside one never renumbers the other's sites.
    """
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _scan_extended_shapes(
    tree: ast.Module,
    module: str,
    rel: str,
    names: dict[str, float],
    local: dict[str, float],
) -> list[NumberSite]:
    """Rules (c), (d) and (e): the shapes rules (a)/(b) cannot see.

    Applied only inside a scoped module — never to `src/config.py`'s named
    classes and never to the unscoped sentinel, whose count is defined as
    module-level constants and would otherwise jump for no reason.
    """
    sites: list[NumberSite] = []
    for node, qual in _qualified_scopes(tree):
        # (c) numeric defaults on function and method parameters.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            pairs += [
                (arg, default)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults)
                if default is not None
            ]
            for arg, default in pairs:
                base = f"{module}.{qual}({arg.arg})"
                for site_id, value, lineno in _leaves(default, base, names, local):
                    if value not in NEUTRAL_VALUES:
                        sites.append(NumberSite(site_id, rel, lineno or node.lineno, value))

            # (e) inline multiplier/divisor literals in the near-one band.
            ordinal = 0
            found: list[tuple[int, int, float]] = []
            for inner in _own_nodes(node):
                if not isinstance(inner, ast.BinOp) or not isinstance(
                    inner.op, (ast.Mult, ast.Div)
                ):
                    continue
                for side, other in ((inner.left, inner.right), (inner.right, inner.left)):
                    if _numeric(other) is not None:
                        # Constant arithmetic (`365 * 5`) is one folded number,
                        # not a margin applied to a price.
                        continue
                    for operand in _factor_operands(side):
                        value = _numeric(operand)
                        # +-1 is the identity or a sign flip, never a margin.
                        if value is None or abs(value) == 1.0:
                            continue
                        low, high = FACTOR_BAND
                        if low <= abs(value) < high:
                            found.append((operand.lineno, operand.col_offset, value))
            for lineno, _col, value in sorted(found):
                sites.append(
                    NumberSite(f"{module}.{qual}:factor[{ordinal}]", rel, lineno, value)
                )
                ordinal += 1

        # (d) numeric attributes on any class. A `*Config` class's annotated
        # fields are rule (b) already; its un-annotated attributes are not,
        # so those are picked up here too.
        if isinstance(node, ast.ClassDef):
            is_config = node.name.endswith(CONFIG_CLASS_SUFFIX)
            for body_node in node.body:
                if isinstance(body_node, ast.AnnAssign):
                    if is_config or not isinstance(body_node.target, ast.Name):
                        continue
                    default = _field_default(body_node)
                    targets = [body_node.target]
                elif isinstance(body_node, ast.Assign):
                    default = body_node.value
                    targets = [t for t in body_node.targets if isinstance(t, ast.Name)]
                else:
                    continue
                if default is None:
                    continue
                for target in targets:
                    base = f"{module}.{qual}.{target.id}"
                    for site_id, value, lineno in _leaves(default, base, names, local):
                        if value not in NEUTRAL_VALUES:
                            sites.append(
                                NumberSite(site_id, rel, lineno or body_node.lineno, value)
                            )
    return sites


def _scoped_files(root: Path) -> list[Path]:
    """Every file `SCOPED_PATHS` names, raising if one has vanished."""
    files: list[Path] = []
    for entry in SCOPED_PATHS:
        target = root / entry
        if target.is_dir():
            found = sorted(target.rglob("*.py"))
        elif target.is_file():
            found = [target]
        else:
            # A scoped path that no longer exists is a finding in itself:
            # scope silently narrowing is how this check would rot.
            raise FileNotFoundError(f"SCOPED_PATHS entry does not exist: {entry}")
        files.extend(found)
    return files


def collect_sites(repo_root: Path | None = None) -> list[NumberSite]:
    """Every in-scope numeric definition site in the tree, sorted by id."""
    root = repo_root or REPO_ROOT
    sites: list[NumberSite] = []

    for file_path in _scoped_files(root):
        if file_path.name == "__init__.py" and file_path.stat().st_size == 0:
            continue
        sites.extend(
            _scan_module(file_path, str(file_path.relative_to(root)), None, root)
        )

    config_module = root / SCOPED_CONFIG_MODULE
    if not config_module.is_file():
        raise FileNotFoundError(f"missing {SCOPED_CONFIG_MODULE}")
    sites.extend(
        _scan_module(config_module, SCOPED_CONFIG_MODULE, SCOPED_CONFIG_CLASSES, root)
    )

    return sorted(sites, key=lambda s: s.site_id)


def collect_unscoped_sites(repo_root: Path | None = None) -> list[NumberSite]:
    """The same scan over every `src/**.py` that is NOT in scope.

    This is the sentinel behind the scope rule. Scope is a reviewed list, and
    a list has no mechanical guarantee of completeness — a test can pin that
    it does not shrink but nothing pins that it is whole. Counting what is
    outside it turns "somebody should widen scope" into a build failure the
    day a new unscoped constant appears.
    """
    root = repo_root or REPO_ROOT
    in_scope = {p.resolve() for p in _scoped_files(root)}
    in_scope.add((root / SCOPED_CONFIG_MODULE).resolve())

    sites: list[NumberSite] = []
    for file_path in sorted((root / "src").rglob("*.py")):
        rel = str(file_path.relative_to(root))
        if file_path.resolve() in in_scope:
            continue
        if any(rel.startswith(prefix) for prefix in UNSCOPED_SENTINEL_EXCLUDE):
            continue
        try:
            sites.extend(_scan_module(file_path, rel, (), root))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
    return sorted(sites, key=lambda s: s.site_id)


def load_ledger(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ledger as `{site_id: entry}`. Raises on a duplicate id."""
    ledger_path = path or LEDGER_PATH
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("numbers") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        site_id = entry.get("id")
        if not site_id:
            raise ValueError(f"ledger entry with no id: {entry!r}")
        if site_id in out:
            raise ValueError(f"duplicate ledger id: {site_id}")
        out[site_id] = entry
    return out


def _appconfig_sections(root: Path) -> dict[str, str]:
    """`{ConfigClassName: settings.yaml section}` read from `AppConfig`.

    Read rather than hardcoded: the mapping IS the field name on `AppConfig`,
    so a renamed section cannot desynchronise this check from the loader.
    """
    tree = ast.parse((root / SCOPED_CONFIG_MODULE).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "AppConfig":
            continue
        out: dict[str, str] = {}
        for body_node in node.body:
            if (
                isinstance(body_node, ast.AnnAssign)
                and isinstance(body_node.annotation, ast.Name)
                and isinstance(body_node.target, ast.Name)
            ):
                out[body_node.annotation.id] = body_node.target.id
        return out
    return {}


def deployed_values(root: Path | None = None) -> dict[str, float]:
    """`{site_id: deployed value}` for every ledger site `settings.yaml` sets.

    THE BLIND SPOT THIS CLOSES. The ledger pins the CODE DEFAULT. For a
    `src.config.*Config.<field>` site the deployed value comes from
    `config/settings.yaml`, so `risk.max_position_risk_pct: 5` could be edited
    to `10` with the gate entirely silent. Measured 2026-09-18: 52 sites route
    this way. They all currently agree with their defaults — which is exactly
    why this is cheap to start enforcing now.
    """
    base = root or REPO_ROOT
    settings = base / "config" / "settings.yaml"
    if not settings.is_file():
        raise FileNotFoundError(str(settings))
    sections = _appconfig_sections(base)
    raw = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    out: dict[str, float] = {}
    for class_name, section in sections.items():
        block = raw.get(section)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[f"src.config.{class_name}.{key}"] = float(value)
    return out


#: A `source` a non-author can open in under a minute: a URL, or a repo path
#: with a line number. Prose is not falsifiable — the four false claims in the
#: entry this gate was built around were all prose, and all were one grep from
#: being disproved.
def _is_falsifiable_source(text: str) -> bool:
    if re.search(r"https?://\S+", text):
        return True
    return bool(re.search(r"\b[\w./-]+\.(?:py|yaml|yml|md|json|toml):\d+", text))


#: Every repo path, with optional line or line range, mentioned anywhere in an
#: entry's prose.
_CITATION_RE = re.compile(
    r"\b((?:docs|src|config|tests|scripts)/[\w./-]+\.(?:md|py|yaml|yml|json|toml))"
    r"(?::(\d+)(?:-(\d+))?)?"
)


def broken_citations(
    ledger: dict[str, dict[str, Any]], root: Path
) -> list[tuple[str, str, str]]:
    """Repo citations in the ledger that do not resolve: `(site_id, why, cite)`.

    The cheapest possible defence against the failure that made this rework
    necessary. It does not read a citation and judge it — nothing can — but a
    citation pointing at a file that does not exist, or at a line past the end
    of one, is a claim nobody opened, and that is the whole shape of what went
    wrong. Caught one on the first run: an invented `docs/FRACTIONAL_TRADING.md`
    in a source written during this very rework.
    """
    out: list[tuple[str, str, str]] = []
    line_counts: dict[str, int | None] = {}
    for site_id, entry in ledger.items():
        text = f"{entry.get('note') or ''} {entry.get('source') or ''}"
        for match in _CITATION_RE.finditer(text):
            rel, start, end = match.group(1), match.group(2), match.group(3)
            if rel not in line_counts:
                target = root / rel
                line_counts[rel] = (
                    len(target.read_text(encoding="utf-8").splitlines())
                    if target.is_file()
                    else None
                )
            count = line_counts[rel]
            if count is None:
                out.append((site_id, "no such file", match.group(0)))
                continue
            for lineno in (start, end):
                if lineno and int(lineno) > count:
                    out.append(
                        (site_id, f"line past end of file ({count} lines)", match.group(0))
                    )
    return out


def audit(
    repo_root: Path | None = None, ledger_path: Path | None = None
) -> list[LedgerProblem]:
    """Every reason the build should fail. Empty means the boundary holds."""
    root = repo_root or REPO_ROOT
    sites = collect_sites(root)
    ledger = load_ledger(ledger_path)
    by_id = {site.site_id: site for site in sites}
    problems: list[LedgerProblem] = []

    try:
        deployed = deployed_values(root)
    except FileNotFoundError:  # pragma: no cover - fixture trees have no settings
        deployed = {}

    # 1. COVERAGE. A number in scope with no entry is the whole point.
    for site in sites:
        if site.site_id not in ledger:
            problems.append(
                LedgerProblem(
                    "unsourced",
                    site.site_id,
                    f"new trade-governing number {site.value!r} at "
                    f"{site.path}:{site.lineno} with no entry in "
                    f"config/number_ledger.yaml. Say where it came from, or "
                    f"record it as arbitrary with its open question and cost.",
                )
            )

    # A ledger entry with no site is stale bookkeeping, and left alone it
    # would let coverage look complete while the file drifted.
    for site_id in ledger:
        if site_id not in by_id:
            problems.append(
                LedgerProblem(
                    "orphan",
                    site_id,
                    "ledger entry no longer matches any definition site — the "
                    "number was renamed, moved or deleted. Remove the entry or "
                    "point it at the new site. If the number moved OUT of "
                    "scope, it is still live: bring the file into scope rather "
                    "than dropping the row.",
                )
            )

    for site_id, entry in ledger.items():
        site = by_id.get(site_id)
        status = entry.get("status")

        if status not in VALID_STATUSES:
            problems.append(
                LedgerProblem(
                    "bad-status",
                    site_id,
                    f"status {status!r} is not one of {sorted(VALID_STATUSES)}",
                )
            )
            continue

        # 2. VALUE. The recorded value must still be the live one, so that
        #    changing a number puts its justification back in front of the
        #    person changing it.
        if site is not None:
            recorded = entry.get("value")
            if recorded is None or abs(float(recorded) - site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "value-drift",
                        site_id,
                        f"live value {site.value!r} but the ledger records "
                        f"{recorded!r}. If the number changed on purpose, "
                        f"re-read its justification and update the entry.",
                    )
                )

        # 2b. DEPLOYED VALUE. The ledger pins the code default; the desk
        #     trades the YAML. Both must agree or the ledger is fiction.
        if site_id in deployed and entry.get("value") is not None:
            if abs(deployed[site_id] - float(entry["value"])) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "deployed-drift",
                        site_id,
                        f"config/settings.yaml deploys "
                        f"{deployed[site_id]!r} but the ledger records "
                        f"{entry['value']!r}. The ledger describes the code "
                        f"default; this is the number the desk actually "
                        f"trades. Re-read the justification against the "
                        f"deployed value.",
                    )
                )

        # 3. GROUNDS.
        if status in ("sourced", "instrument"):
            source = str(entry.get("source") or "").strip()
            if not source:
                problems.append(
                    LedgerProblem(
                        "no-source",
                        site_id,
                        f"status {status!r} requires a `source:` saying where "
                        f"the number was read from.",
                    )
                )
            elif not _is_falsifiable_source(source):
                problems.append(
                    LedgerProblem(
                        "unfalsifiable-source",
                        site_id,
                        "`source:` is prose with nothing a non-author can "
                        "open. Give a URL, or a repo `path:line` pointing at "
                        "the derivation. The flagship entry of this ledger was "
                        "false in four places and every claim was one grep "
                        "from being disproved.",
                    )
                )
        if status == "not-trade-governing" and not str(entry.get("note") or "").strip():
            problems.append(
                LedgerProblem(
                    "no-note",
                    site_id,
                    "status 'not-trade-governing' requires a `note:` saying why "
                    "this number cannot reach a trade decision.",
                )
            )
        if status == "arbitrary":
            for field in ARBITRARY_REQUIRED_FIELDS:
                if not str(entry.get(field) or "").strip():
                    problems.append(
                        LedgerProblem(
                            "incomplete-debt",
                            site_id,
                            f"status 'arbitrary' requires `{field}:`. An "
                            f"arbitrary number is a debt, not a status: it "
                            f"must state the open question in a form somebody "
                            f"could answer, and what the desk pays while it is "
                            f"unanswered (docs/OUTCOME.md, outcome 3). It must "
                            f"not be the cheapest entry to write.",
                        )
                    )

        # 4. BASE DRIFT. The class from the brief: sourced once, unsourced
        #    later because what it was derived from moved underneath it.
        if status == "derived":
            base_id = entry.get("derived_from")
            if not base_id:
                problems.append(
                    LedgerProblem(
                        "no-base",
                        site_id,
                        "status 'derived' requires `derived_from:` naming the "
                        "ledger entry this was computed from.",
                    )
                )
                continue
            if "base_value" not in entry:
                problems.append(
                    LedgerProblem(
                        "no-base-value",
                        site_id,
                        "status 'derived' requires `base_value:` — the value "
                        "the base held when this derivation was written. "
                        "Without it a moving base is undetectable.",
                    )
                )
                continue
            base_site = by_id.get(base_id)
            if base_site is None:
                problems.append(
                    LedgerProblem(
                        "unknown-base",
                        site_id,
                        f"derived_from {base_id!r} is not a definition site in "
                        f"scope. A derivation can only be tracked against a "
                        f"number this check can see.",
                    )
                )
                continue
            if abs(float(entry["base_value"]) - base_site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "base-drift",
                        site_id,
                        f"derived from {base_id} when it was "
                        f"{entry['base_value']!r}; it is now {base_site.value!r}. "
                        f"The derivation no longer describes the live geometry, "
                        f"so this number is arbitrary again. Re-derive it, or "
                        f"reclassify it honestly.",
                    )
                )

    # Rules 5 and 6 are properties of THE ledger and THE tree, not of any
    # ledger: a synthetic fixture holding two numbers is not evidence that
    # the desk's arbitrary count moved. They are skipped for a fixture tree so
    # that every OTHER rule stays testable in isolation.
    if ledger_path is not None and ledger_path != LEDGER_PATH:
        return sorted(problems, key=lambda p: (p.kind, p.site_id))

    # 5. RATCHET, checked for EQUALITY. As a ceiling it rewarded deletion:
    #    move a trade constant into an unscoped file, drop its row, and the
    #    build went green with a LOWER arbitrary count than before.
    arbitrary = [i for i, e in ledger.items() if e.get("status") == "arbitrary"]
    if len(arbitrary) != MAX_ARBITRARY_ENTRIES:
        direction = "rises to" if len(arbitrary) > MAX_ARBITRARY_ENTRIES else "falls to"
        problems.append(
            LedgerProblem(
                "ratchet",
                "<ledger>",
                f"the `arbitrary` count {direction} {len(arbitrary)} but "
                f"MAX_ARBITRARY_ENTRIES is {MAX_ARBITRARY_ENTRIES}. This is an "
                f"equality, not a ceiling: sourcing one means lowering the "
                f"number in the same commit, and a row cannot leave the ledger "
                f"without saying so. Adding an unsourced trade-governing "
                f"number is an owner decision (docs/OUTCOME.md).",
            )
        )

    # 7. CITATIONS RESOLVE. Cheap, and aimed squarely at the failure that
    #    made this gate's own flagship entry false in four places.
    for site_id, why, cite in broken_citations(ledger, root):
        problems.append(
            LedgerProblem(
                "broken-citation",
                site_id,
                f"cites {cite} - {why}. A citation nobody can open is not a "
                f"source, and an invented one is worse than none.",
            )
        )

    # 6. UNSCOPED SENTINEL. Scope is a reviewed list; this is what stops the
    #    list from lagging the code silently, and closes the "park it in an
    #    unscoped file" move that rule 5 alone does not.
    try:
        unscoped = collect_unscoped_sites(root)
    except FileNotFoundError:  # pragma: no cover - fixture trees have no src/
        unscoped = []
    if len(unscoped) > MAX_UNSCOPED_NUMERIC_SITES:
        problems.append(
            LedgerProblem(
                "unscoped-growth",
                "<scope>",
                f"{len(unscoped)} module-level numeric constants now sit in "
                f"`src/` files outside SCOPED_PATHS, above the recorded "
                f"{MAX_UNSCOPED_NUMERIC_SITES}. If the new one decides, sizes, "
                f"prices or exits a trade, bring its module into scope and "
                f"ledger it. If it does not, raise this ceiling in the same "
                f"commit and say which constant it is.",
            )
        )

    return sorted(problems, key=lambda p: (p.kind, p.site_id))


def main() -> int:  # pragma: no cover - CLI convenience
    problems = audit()
    if not problems:
        sites = collect_sites()
        unscoped = collect_unscoped_sites()
        print(
            f"number ledger: {len(sites)} sites in scope, all accounted for; "
            f"{len(unscoped)} unscoped constants watched"
        )
        return 0
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} problem(s). See src/number_sources.py for the rules.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
