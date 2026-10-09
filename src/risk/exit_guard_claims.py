"""Holding-discipline claim classifiers, lifted verbatim from
`src/risk/exit_guard.py` (spec item 25).

Phrase-matching over a SELL/REDUCE/COVER's stated reasoning, checked against
the real same-day macro and state-change data handed in as arguments. Every
name is re-exported by `src.risk.exit_guard`; no threshold, comparison or
default changed in the move.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from src.risk.state_change_parser import StateChangeParser


# ---------------------------------------------------------------------------
# Holding-discipline compliance — spec item 25 (2026-09-03)
# ---------------------------------------------------------------------------
#
# WHAT WENT WRONG. `config/prompts/risk_manager.md` ("Holding-discipline
# compliance") asks the AI Risk Manager to itself verify, for every position
# held under 5 days, that a proposed SELL/REDUCE/COVER names one of exactly
# three allowed triggers: (a) a triggered `thesis_invalid_if`, (b) a regime
# flip to risk-off TODAY, or (c) a HIGH-conviction bearish state_change dated
# today that names the symbol. Nothing in Python checked any of this — the
# prompt told the model to grade its own homework against real data it was
# handed in the same message, with no deterministic pass afterward. This is
# the same "citation exists vs. citation is real" shape as the sub-floor
# catalyst gate (`PortfolioManagerAgent._catalyst_cites_state_change`) fixed
# the same day.
#
# UPDATE, 2026-09-03/04 — the flat "held under 5 days" window described
# above is GONE from the code (it never had a backtest behind it, and the
# owner rejected it as arbitrary). What counts as "protected" is now
# `check_structural_protection`'s data-driven answer, defined further down
# this file: intact unless the trade's own `thesis_invalid_if` or the
# structural level backing its stop has broken on the CLOSE of a second
# consecutive trading day (a same-day close is not enough — see
# `check_structural_protection`'s docstring on why: a "spring" false
# breakdown is a real, well-documented pattern), with a noise-band fallback
# (not an automatic unprotect) when neither exists. `holding_discipline_false_claim`
# below takes that answer as a plain `protected: bool` — everything in this
# comment block about (a)/(b)/(c) and what gets checked is otherwise
# unchanged.
#
# SCOPE — (b) and (c) ONLY. (a) is explicitly NOT verified here: evaluating
# an arbitrary free-text `thesis_invalid_if` condition against live price
# data is a real, separate feature (parsing "closes below the 50-day" or
# "loses the $142 level" into an executable check), and guessing at it would
# be worse than not attempting it. The only (a)-adjacent question this module
# *could* safely answer — "was a non-empty thesis_invalid_if actually
# recorded at entry, as opposed to fabricated after the fact" — turned out to
# have no reliable answer either: the only place an entry's
# `thesis_invalid_if` survives is embedded as free text inside the BUY's
# stored `TradeDecision.reasoning` (`PortfolioConstructor._build_buy` writes
# "... (invalid if: <text>)"; `_build_short`/`_build_sell` use a *different*
# "(thesis_invalid_if: <text>)" phrasing — the two builders do not even agree
# on the marker string), and that whole field is truncated to 500 characters
# at write time and again to 280 by `TradingPipeline._build_position_history`
# before anything downstream ever sees it. A long thesis can push the
# marker past either truncation point, which would make "no invalid_if
# found" indistinguishable from "one was recorded but cut off" — exactly the
# false-negative a discipline check must not manufacture. So (a) stays
# entirely unaddressed here, deliberately, and a SELL relying on it is never
# penalized by anything below for that reason alone.
#
# DESIGN — because (a) cannot be checked, a SELL that fails to prove (b) or
# (c) is NOT thereby suspect: it may be a perfectly legitimate (a)-based
# exit this module simply has no visibility into. Blocking on "(b) and (c)
# both come up empty" would veto every honest invalidation-based exit, which
# is worse than the discipline gap this fixes. The only thing this module
# ever acts on is a POSITIVELY CONTRADICTED claim: the decision's own
# reasoning text asserts a specific, checkable fact ("regime flipped to
# risk-off", "high-conviction bearish state change") and the verifiable data
# for TODAY says otherwise. That is provable dishonesty, not an unprovable
# gap, and is the only thing `holding_discipline_false_claim` flags.
#
# UPDATE, 2026-09-04 — a PROVEN-FALSE claim now BLOCKS the exit (owner
# approved). This module previously only ever LOGGED, and the "FLAGGED FOR
# REVIEW: should this ever escalate to a veto" question recorded here has
# been answered: yes, and only for `verdict == "false"`. What did NOT change
# is the bar for reaching that verdict — every reason the old note gave for
# caution is a reason the FALSE bar stays exactly where it was, not a reason
# to keep the finding toothless:
#
#   - claim-detection is phrase matching over free text (the two regexes
#     below plus `_is_negated`'s short negation-cue guard). Adversarial
#     review (2026-09-03) produced a concrete reproducing sentence — "No
#     regime shift to risk-off has occurred; exiting purely on
#     thesis_invalid_if" was matched as CLAIMING a flip before the negation
#     guard existed. `_is_negated` closes that specific, demonstrated case;
#     it is a short common-word list, not a general negation parser. So the
#     veto only ever fires when the text asserts a claim AND real recorded
#     data for TODAY affirmatively says the opposite.
#   - anything that merely CANNOT be checked (macro untrusted this run, or
#     no same-day state-change row names the symbol at all) is verdict
#     "unverifiable": still logged and recorded as a pipeline event, exactly
#     as before, and NEVER blocked and never alerted on. Absence of proof
#     stays absence of proof. This is the owner's explicit instruction and
#     it is the whole reason the verdict is three-valued rather than a bool.
#   - (a) `thesis_invalid_if` is still never evaluated here, so a SELL
#     resting on it is never touched by any of this.
#
# The residual risk the old note named is real and accepted with eyes open:
# a correctly-read false (b)/(c) claim does not by itself prove the SELL is
# wrong, because an unverifiable (a) might independently justify it. The
# owner's call is that an exit whose *stated* justification is provably
# contradicted by the desk's own recorded data should not execute on that
# justification — the RM can re-propose it next cycle citing something true.
# Every block fires a standalone owner alert (see `RiskStage`) precisely so
# the frequency of that trade-off is measured, not assumed.

#: Phrases that assert a regime flip to risk-off. Deliberately the same two
#: patterns `EXTERNAL_INFORMATION_PATTERNS` already uses for the identical
#: concept (regime shift / risk-off), so "does this reason claim a regime
#: flip" is answered identically everywhere in this module rather than by a
#: second, silently-diverging definition.
_REGIME_FLIP_CLAIM_RE = re.compile(
    r"\bregime (?:shift|flip|flipped)\b|\brisk[- ]off\b",
    re.IGNORECASE,
)

#: Phrases that assert a HIGH-conviction bearish state_change. Same two
#: "high[-]conviction bearish" / "high bearish" patterns as
#: `EXTERNAL_INFORMATION_PATTERNS`, plus the literal "bearish state change"
#: phrasing the risk_manager.md checklist item itself uses.
_BEARISH_STATE_CHANGE_CLAIM_RE = re.compile(
    r"\bhigh[- ]?conviction bearish\b|\bhigh bearish\b|\bbearish state change\b",
    re.IGNORECASE,
)

#: `data_status["macro"]` values that mean "this run's macro_analysis is a
#: real reading dated TODAY" — as opposed to absent, failed, or parse-error.
#: Reuses `TradingPipeline._carry_forward_macro` / `build_evidence_registry`'s
#: own distinction (see their docstrings) rather than inventing a second one:
#: "carried_from_morning" is explicitly this morning's read of TODAY, refused
#: by the producer itself whenever the stored state is not dated today, so it
#: is exactly as trustworthy as "ok" for this purpose.
TRUSTED_MACRO_STATUSES = frozenset({"ok", "carried_from_morning"})

#: Phrases that assert the trade's thesis has been INVALIDATED. Deliberately
#: the same five phrases `pipeline._HARD_TRIGGER_KEYWORDS` accepts under its
#: "Thesis invalidation" heading and no others, so "does this reason claim a
#: thesis invalidation" is answered by one definition rather than two that
#: can silently diverge.
#:
#: Why this predicate exists at all (2026-09-14, docs/WORK.md item 60). Of
#: the 26 hard-trigger keywords, 21 also match
#: `EXTERNAL_INFORMATION_PATTERNS` and therefore skip the ATR noise band
#: outright; these five are the only ones that do not. Thesis invalidation
#: is thus the entire non-redundant domain of that band — and it was also
#: the one exit class on which `check_structural_protection` was never
#: consulted, because the intraday assembler short-circuited on the absence
#: of a (b)/(c) claim. "Has the level backing this stop closed beyond it on
#: two consecutive sessions" is a checkable fact and is the actual question
#: a thesis-invalidation exit is asserting an answer to; "this move is
#: bigger than one ATR" is not an answer to the same question.
#:
#: Over-matching here is safe BY CONSTRUCTION and under-matching is not: a
#: match only buys a read-only structural read plus an audit row, and can
#: neither block an exit nor release one.
_THESIS_INVALIDATION_CLAIM_RE = re.compile(
    # No trailing \b after "invalid": the keyword list matches by plain
    # substring, so "thesis_invalid_if triggered" and "thesis invalidated"
    # are both hard triggers today and must both be recognised here.
    r"\bthesis[_ ]invalid|\binvalidation triggered\b|"
    r"\bbroken thesis\b|\bthesis broken\b",
    re.IGNORECASE,
)

#: A negation cue in the ~6 words immediately before a matched phrase flips
#: what the phrase means — "regime shift to risk-off" asserts one, "NO
#: regime shift to risk-off has occurred" denies it, and the bare pattern
#: cannot tell them apart. Found by adversarial review (2026-09-03) with a
#: concrete reproducing sentence, not a theoretical gap: without this guard,
#: a SELL reasoning that explicitly DENIES a regime flip or a bearish state
#: change gets misread as CLAIMING one, and — if today's real data happens
#: to disagree with the denied claim — produces a "contradiction" finding
#: for a decision whose reasoning never actually contradicted anything.
#: Deliberately a short, common word list, not a general negation parser:
#: this module already stops short of veto power precisely because
#: phrase-matching cannot fully understand text, and a fancier negation
#: detector would just move the same risk to different sentences rather
#: than remove it. This closes the demonstrated case; it does not claim to
#: close every case.
_NEGATION_CUE_RE = re.compile(
    r"\b(?:no|not|never|isn'?t|wasn'?t|hasn'?t|didn'?t|doesn'?t|without|"
    r"lack(?:ing|s)? of|absent(?:\s+any)?|no\s+evidence\s+of)\b",
    re.IGNORECASE,
)

#: How many characters before a matched claim to scan for a negation cue.
#: ~6 words at typical reasoning-sentence length; wide enough to catch "no
#: regime shift to risk-off has occurred" (cue precedes the match by ~28
#: chars) without reaching back into an unrelated prior clause.
_NEGATION_LOOKBACK_CHARS = 40


def _is_negated(text: str, match: re.Match) -> bool:
    window = text[max(0, match.start() - _NEGATION_LOOKBACK_CHARS) : match.start()]
    return bool(_NEGATION_CUE_RE.search(window))


def claims_regime_flip(reason: str) -> bool:
    """True when `reason` asserts (not denies) a regime flip to risk-off."""
    if not reason:
        return False
    match = _REGIME_FLIP_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


def claims_bearish_state_change(reason: str) -> bool:
    """True when `reason` asserts (not denies) a HIGH-conviction bearish
    state_change."""
    if not reason:
        return False
    match = _BEARISH_STATE_CHANGE_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


def claims_thesis_invalidation(reason: str) -> bool:
    """True when `reason` asserts (not denies) that the thesis is invalid.

    Purely a ROUTING predicate — see `_THESIS_INVALIDATION_CLAIM_RE`. It
    decides whether the structural check is worth consulting and recording
    for this exit; it is deliberately NOT an input to
    `holding_discipline_claim_check`, which continues to leave (a)
    `thesis_invalid_if` unjudged. Nothing gates a block or a release on
    this function.
    """
    if not reason:
        return False
    match = _THESIS_INVALIDATION_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


@dataclass(frozen=True)
class HoldingDisciplineClaimCheck:
    """Three-valued verdict on a SELL/REDUCE/COVER's stated (b)/(c) trigger.

    The three-valued shape is the whole point, and is the owner's explicit
    2026-09-04 instruction: a claim the desk's own recorded data
    AFFIRMATIVELY CONTRADICTS is a different thing from a claim the desk
    simply could not check this run, and only the first may block a trade.
    Collapsing the two into one bool is exactly how "we could not verify it"
    turns into "we proved it false", which would veto honest exits.

    `verdict`:
      "ok"           - no checkable (b)/(c) claim was made, or every claim
                       made was CONFIRMED by real data, or the decision is
                       out of scope (not an exit / not a protected position).
                       Nothing is logged, nothing blocks.
      "unverifiable" - a (b)/(c) claim WAS made but the data needed to judge
                       it is not available this run (macro status outside
                       `TRUSTED_MACRO_STATUSES`, or no same-day state-change
                       row names the symbol at all). LOGGED ONLY: never
                       blocks, never alerts. Absence of proof is not proof.
      "false"        - a (b)/(c) claim was made and real recorded data for
                       TODAY says the opposite. BLOCKS the decision and
                       fires a standalone owner alert.

    `finding` is the human-readable audit-trail sentence (None when
    `verdict` is "ok"). `reasons` holds the individual contradiction or
    unverifiability clauses, so an alert can name them without re-parsing
    the rendered sentence.
    """

    verdict: Literal["ok", "unverifiable", "false"]
    finding: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def blocks(self) -> bool:
        """True only for a PROVEN-FALSE claim. The one thing callers gate a
        veto on — deliberately not `finding is not None`, which would also
        be true for the log-only unverifiable case."""
        return self.verdict == "false"


def holding_discipline_claim_check(
    *,
    action: str,
    reason: str,
    symbol: str,
    protected: bool,
    macro_regime_today: str | None,
    macro_status: str | None,
    state_change_parser: StateChangeParser,
    active_state_changes: str = "",
    asof: date | None = None,
    exit_trigger: object = None,
) -> HoldingDisciplineClaimCheck:
    """Judge whether a PROTECTED position's exit states a (b)/(c) trigger
    that real recorded data CONTRADICTS, merely cannot CHECK, or CONFIRMS.

    `protected` replaces the old flat `days_held < 5` gate (owner decision,
    2026-09-03/04 — see `check_structural_protection`'s module note for the
    full replacement rationale). The caller computes it once via
    `check_structural_protection(...).protected` — data-driven and no
    longer time-bound at all — and passes the single bool in here; this
    function itself only decides whether the STATED (b)/(c) trigger is
    provably real.

    Checks ONLY:
      (b) a claimed regime flip to risk-off. CONTRADICTED when today's macro
          read (`macro_status` in `TRUSTED_MACRO_STATUSES`) shows a
          DIFFERENT, non-risk-off regime; UNVERIFIABLE when the macro status
          is not trusted this run or no regime was read at all.
      (c) a claimed HIGH-conviction bearish state_change. CONTRADICTED when a
          same-day `active_state_changes` row DOES name the symbol but with a
          recorded direction that is NOT bearish (parsed via
          the injected `state_change_parser` (PM agent's, never imported here), the exact
          function that already owns this parsing for the sub-floor catalyst
          gate — not reimplemented here); UNVERIFIABLE when no same-day row
          names the symbol at all, because the news pipeline can simply not
          have logged a real catalyst as a formal `state_change` row yet.

    Returns verdict "ok" (nothing to say) for:
      - an action other than SELL/REDUCE/COVER;
      - a position that is not currently `protected` (its thesis-backing
        level has broken and been confirmed, or it has no basis and is
        outside the noise band) — a plain SELL there needs no special
        justification, so nothing here is worth checking;
      - a reason that makes neither claim;
      - a claim real data CONFIRMS;
      - (a) `thesis_invalid_if` — not itself re-evaluated here (it already
        fed into `protected` upstream), so a SELL resting entirely on it is
        never flagged just because (b) and (c) are absent or unverifiable.

    A "false" verdict is a veto (see the module note above for the owner
    decision and the accepted trade-off). An "unverifiable" verdict is an
    audit-trail record and nothing more.
    """
    if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
        return HoldingDisciplineClaimCheck("ok")
    if not protected:
        return HoldingDisciplineClaimCheck("ok")
    reason = reason or ""
    symbol_u = symbol.strip().upper()
    contradictions: list[str] = []
    unverifiable: list[str] = []

    # 2026-09-18: which claim is being made is read from the STRUCTURED
    # trigger first and from the prose only as a fallback. That is the
    # whole point of `PositionAction.exit_trigger`. Before it existed this
    # function asked two regexes what the sentence claimed, so the two
    # real 2026-09-16 exits — whose entire reason was the words "adverse
    # news" — made no claim either regex recognised, returned "ok", and
    # were never checked against anything.
    #
    # `adverse_news` is routed to the same-day state-change check because
    # that is the record the claim is ABOUT: an adverse news event naming
    # this symbol today. `sector_shock` is deliberately NOT routed here —
    # it is a sector-level assertion and the desk records no sector-shock
    # row, so pointing a symbol-level check at it would manufacture an
    # "unverifiable" on every such exit and tell the reviewer nothing.
    from src.risk.exit_trigger import ExitTrigger, normalize_trigger

    _trigger = normalize_trigger(exit_trigger)
    _claims_regime = claims_regime_flip(reason) or _trigger is ExitTrigger.REGIME_SHIFT
    _claims_bearish = claims_bearish_state_change(reason) or _trigger in (
        ExitTrigger.BEARISH_STATE_CHANGE,
        ExitTrigger.ADVERSE_NEWS,
    )

    if _claims_regime and not (_claims_bearish or claims_thesis_invalidation(reason)):
        # Owner mandate 2026-10-09 (docs/OUTCOME.md): each stock's own
        # behaviour decides; market mood is one input, never the decider.
        # Control only reaches here when `protected` is True — the existing
        # per-name check says this name's own thesis-backing level has NOT
        # broken. A sell resting on the regime alone, with no claim about the
        # name itself, therefore rests on market mood alone and is refused
        # whatever today's macro read says. It can be re-proposed citing the
        # name's own evidence (a bearish state change or thesis invalidation).
        contradictions.append(
            "rests on a regime shift to risk-off alone, but market mood cannot "
            "decide an exit by itself and this name's own structural level is "
            "intact (position still protected); no name-level evidence was cited"
        )
    elif _claims_regime:
        if macro_status in TRUSTED_MACRO_STATUSES and macro_regime_today:
            if macro_regime_today != "risk-off":
                contradictions.append(
                    f"claims a regime flip to risk-off today, but today's "
                    f"macro read ({macro_status}) shows regime="
                    f"{macro_regime_today!r}, not risk-off"
                )
            # else: the claim is CONFIRMED — say nothing.
        else:
            # Macro unavailable/untrusted this run. Recorded so the gap is
            # visible in the audit trail, but never blocked and never
            # alerted on: this is the exact case the owner separated out.
            unverifiable.append(
                f"claims a regime flip to risk-off today, but this run's "
                f"macro read (status={macro_status!r}) cannot confirm or "
                f"deny it"
            )

    if _claims_bearish:
        # Named after the claim actually made, so an alert reads truthfully
        # whether the trigger arrived as prose or as a structured field.
        claim_label = (
            "an adverse news event naming it"
            if _trigger is ExitTrigger.ADVERSE_NEWS and not claims_bearish_state_change(reason)
            else "a HIGH-conviction bearish state change"
        )
        by_date = state_change_parser(active_state_changes, asof)
        try:
            from src.trading_calendar import et_today

            today_iso = str(asof) if asof is not None else str(et_today())
        except Exception:  # pragma: no cover - clock/tz failure
            today_iso = None
        directions = by_date.get(today_iso) if today_iso else None
        symbol_directions = directions.get(symbol_u) if directions else None
        if symbol_directions is None:
            # No same-day row names the symbol at all -> unverifiable, not
            # false. A missing state-change row is much weaker evidence than
            # a definite non-matching macro regime: the news pipeline can
            # simply not have logged a real catalyst as a formal row yet, so
            # treating "not found" as "false" would manufacture false
            # positives on legitimate exits.
            unverifiable.append(
                f"claims {claim_label} today, but no same-day Active News State Change row names {symbol_u} either way"
            )
        elif "bearish" not in symbol_directions:
            rendered = ", ".join(sorted(symbol_directions)) or "no recorded direction"
            contradictions.append(
                f"claims {claim_label} today, but "
                f"today's Active News State Change block names {symbol_u} "
                f"with direction(s) {rendered} instead of bearish"
            )
        # else: the claim is CONFIRMED — say nothing.

    if contradictions:
        # A contradiction outranks any co-occurring unverifiable clause: one
        # provably false claim is enough, and mixing "we could not check the
        # other one" into the same verdict would only muddy it.
        return HoldingDisciplineClaimCheck(
            "false",
            f"{symbol_u}: {action} on a structurally-protected position — "
            f"reasoning "
            + "; and ".join(contradictions)
            + f". This is a provable contradiction of a checkable claim: the "
            f"exit is BLOCKED on the justification given. thesis_invalid_if "
            f"(which this module cannot verify either way) may independently "
            f"justify this exit — if so it can be re-proposed citing that.",
            tuple(contradictions),
        )
    if unverifiable:
        return HoldingDisciplineClaimCheck(
            "unverifiable",
            f"{symbol_u}: {action} on a structurally-protected position — "
            f"reasoning "
            + "; and ".join(unverifiable)
            + f". NOT treated as false and NOT blocked — absence of proof is "
            f"not proof of a false claim. Recorded for review only.",
            tuple(unverifiable),
        )
    return HoldingDisciplineClaimCheck("ok")


def holding_discipline_false_claim(
    *,
    action: str,
    reason: str,
    symbol: str,
    protected: bool,
    macro_regime_today: str | None,
    macro_status: str | None,
    state_change_parser: StateChangeParser,
    active_state_changes: str = "",
    asof: date | None = None,
) -> str | None:
    """Return the finding string for a PROVABLY FALSE holding-discipline
    claim, else None.

    Thin wrapper over `holding_discipline_claim_check` kept because
    "provably false, or nothing" is genuinely the question most callers
    want, and because collapsing it here — in one place, explicitly on
    `verdict == "false"` — is safer than letting each caller decide what
    counts as false. An UNVERIFIABLE claim returns None here by design: use
    `holding_discipline_claim_check` directly to see (and log) that case.
    """
    result = holding_discipline_claim_check(
        action=action,
        reason=reason,
        symbol=symbol,
        protected=protected,
        macro_regime_today=macro_regime_today,
        macro_status=macro_status,
        state_change_parser=state_change_parser,
        active_state_changes=active_state_changes,
        asof=asof,
    )
    return result.finding if result.blocks else None
