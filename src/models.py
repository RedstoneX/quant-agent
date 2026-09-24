import logging
import math
import re
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, date
from typing import Annotated, Any, Literal, get_origin

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter, ValidationInfo, computed_field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from src.quantities import collapse_stances
# `src.risk.exit_trigger` imports only the stdlib (and `src.risk` has an
# empty __init__), so this cannot cycle back through models.
from src.risk.exit_trigger import ExitTrigger, normalize_trigger

logger = logging.getLogger(__name__)


def reward_to_risk(
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    *,
    is_short: bool,
) -> float | None:
    """THE reward:risk of an entry. One definition, every caller.

    Every place in this codebase that divides a reward by a risk goes
    through this function. That is the whole point of it, and it is a
    correction of a measured failure rather than a tidiness exercise.

    On 2026-09-01 the Risk Manager rejected a live XLE BUY with the words
    *"PM's reasoning assumes R/R 1.67 but the executed order has R/R
    1.18"*. Both numbers were arithmetically correct and neither stop had
    moved: 1.67 was `TechAnalysisResult.risk_reward`, computed at the
    analyst's own snapshot entry $63.96 against its own guessed target
    $68.00; 1.18 was `TradeDecision.reward_risk`, computed at the live
    entry $64.51 the constructor actually priced against the structural
    target the constructor actually derived. On that particular trade the
    two targets happened to coincide at $68.00, so the ENTIRE gap was the
    entry: same trade, same stop ($61.54), two entries, four independent
    copies of the division. On 2026-08-31 the same seat caught the same
    thing on XLE
    again — *"entry price degradation from TechAnalyst's $62.29 to
    $63.76"*. A ratio the desk gates on must not be re-derived by hand at
    each site; when it is, the sites disagree and the disagreement itself
    starts rejecting trades.

    FAIL CLOSED. Returns None — "this is not a measurable entry geometry"
    — for anything malformed, and non-finite input is malformed. That
    matters more than it looks: a NaN price propagates silently through
    `reward / risk` and every subsequent `ratio < floor` comparison is
    False, so a NaN would WAVE A TRADE THROUGH a floor it cannot satisfy.
    Callers must treat None as "cannot judge" and refuse rather than
    permit wherever the geometry was supposed to exist.

    Returns the UNROUNDED ratio. Rounding is a display concern and belongs
    at the edge; rounding before a comparison is how 1.4951 renders as
    "1.5" to a reader while failing a 1.5 gate.
    """
    values = (entry_price, stop_price, target_price)
    if any(v is None for v in values):
        return None
    try:
        entry = float(entry_price)   # type: ignore[arg-type]
        stop = float(stop_price)     # type: ignore[arg-type]
        target = float(target_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (entry, stop, target)):
        return None
    if entry <= 0 or stop <= 0 or target <= 0:
        return None
    if is_short:
        risk = stop - entry
        reward = entry - target
    else:
        risk = entry - stop
        reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    ratio = reward / risk
    if not math.isfinite(ratio):
        return None
    return ratio


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("symbol cannot be empty")
    return symbol


def _normalize_enum_case_fields(
    values,
    *,
    lower_fields: tuple[str, ...] = (),
    upper_fields: tuple[str, ...] = (),
):
    """Case-fold dict fields before Pydantic Literal validation.

    Pydantic ``Literal["high", "medium", "low"]`` is exact-match —
    ``"HIGH"`` or ``"Medium"`` raises ValidationError, which on the
    tech_analyst path silently drops that symbol's analysis (the
    chunk-level except catches and logs but does not surface
    upstream). Most prompts give examples in the expected case but
    LLMs occasionally drift, especially after a long CoT. Folding
    input case before the Literal check turns a cosmetic drift from
    "whole symbol lost" into a no-op.

    Only touches string values; non-string inputs (None / numbers /
    lists / dicts) pass through unchanged so Pydantic's own type
    errors still surface for genuinely malformed inputs.
    """
    if not isinstance(values, dict):
        return values
    for name in lower_fields:
        v = values.get(name)
        if isinstance(v, str):
            values[name] = v.strip().lower()
    for name in upper_fields:
        v = values.get(name)
        if isinstance(v, str):
            values[name] = v.strip().upper()
    return values


# ---------------------------------------------------------------------------
# Explicit-null tolerance for fields that already declare a default
# ---------------------------------------------------------------------------
#
# MEASURED (2026-09-01/02, against the production agent_logs snapshot covering
# 2026-08-14..2026-09-01):
#
#   TechAnalysisResult.thesis_invalid_if          42 nulls / 2,021 occurrences
#   MissedOpportunity.theme_durability            25 nulls /    50 occurrences
#   MissedOpportunity.universe_addition_reason    11 nulls /    50 occurrences
#
# All three are non-Optional fields WITH a default. Pydantic validates the
# declared type before any mode="after" model validator runs, so an explicit
# `null` is a type error and the WHOLE object is rejected — the analysis, the
# missed-opportunity entry, everything on it. A field-level `| None` sibling
# on the same object tolerates the identical input. The distinction is
# invisible to the model producing the JSON and carries no meaning: "I have
# nothing to say here" is what an omitted key already means, and an omitted
# key takes the default without complaint.
#
# Scope, stated honestly (checked before relying on it). The 42 nulls span 28
# distinct symbols across 4 responses. FOUR were lost permanently — EQNR
# (2026-08-20) and AMT/EQIX/PLD (2026-08-25), matching those batches' own
# "1 failed" / "3 failed" lines exactly. The other 24 were rescued by a
# bounded retry: a paid extra call each, invisible afterwards because a
# rescued batch reports data_status "ok". But every one of the 42 was rated
# `neutral`, so no TRADEABLE candidate has been shown lost, and the
# zero-trade day of 2026-09-01 (which lost nothing — 58/58) has a different
# cause. The exposure and those 4 analyses justify the fix; a lost trade does
# not, and must not be claimed.
#
# A static sweep of src/models.py found 119 fields with this exact shape, so
# patching them one `field_validator` at a time is a losing race — the next
# field the model decides to null is not on anybody's list. This is the
# mechanical version: any field that declares a default treats an explicit
# null as an ABSENT key, which is a state the schema already declares legal
# and production already exercises constantly.
#
# What this deliberately does NOT do:
#   * REQUIRED fields are untouched. A null in `symbol`, `rating`,
#     `reasoning`, `reasoning_chain`, `TradeDecision.stop_loss`,
#     `SellGrade.sell_price` etc. still rejects the object, which is correct:
#     no default exists, so there is nothing safe to fall back to.
#   * `X | None` fields are untouched — they already accept null.
#   * mode="after" validators still run unchanged. An actionable
#     TechAnalysisResult with a nulled `support_levels` still fails
#     `_validate_rating_price_consistency` ("requires at least one structural
#     level"). Null-tolerance never manufactures a tradeable analysis.
#   * `_NULL_MUST_FAIL` (below) keeps null fatal on the handful of defaulted
#     fields whose default is an affirmative instruction rather than an
#     "unknown/empty" marker.
#
# Sibling of `_normalize_enum_case_fields` above and the same argument: a
# cosmetic difference in how the model spells "nothing" must not cost the desk
# a whole candidate.


class AnalysisParseTelemetry:
    """Per-run tally of what parsing lost or had to paper over.

    Coercion without counting is exactly the failure this fix is supposed to
    stop: `thesis_invalid_if` is the SOFT-EXIT signal ("what would prove this
    thesis wrong"), so silently substituting a blank keeps the analysis but
    throws a real risk-management input away, and nothing anywhere would say
    so. Recovering the object is right; recovering it quietly is not.

    Thread-safe because the morning research stage validates tech, macro,
    news and earnings responses concurrently in a ThreadPoolExecutor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter = Counter()
        self._drops: Counter = Counter()
        self._local = threading.local()

    @property
    def _suspended(self) -> bool:
        return getattr(self._local, "suspended", 0) > 0

    @contextmanager
    def suspended(self):
        """Don't tally anything validated inside this block.

        Three call sites (`PortfolioManagerAgent._drop_invalid_targets`,
        `EveningAnalystAgent._drop_invalid_entries` and
        `._drop_invalid_missed_opportunities`) pre-validate each list item to
        decide keep-or-drop, then hand the SURVIVING raw dicts to the parent
        model, which validates them a second time. Without this the operator's
        count would be double the number of objects actually affected, and a
        number the operator has to mentally halve is a number they will stop
        reading.

        Thread-local: a real parse running concurrently in another research
        thread still counts.
        """
        self._local.suspended = getattr(self._local, "suspended", 0) + 1
        try:
            yield
        finally:
            self._local.suspended -= 1

    def record_null_coercion(self, model_name: str, field_name: str) -> None:
        """A defaulted field arrived as an explicit null and took its default."""
        if self._suspended:
            return
        with self._lock:
            self._counts[(model_name, field_name)] += 1

    def record_dropped_item(self, model_name: str, key: str) -> None:
        """A whole parsed item was discarded — `key` is the symbol where known.

        This is the loss the null-tolerance rule above is designed to prevent,
        counted separately so "we kept it but blanked a field" is never
        confused with "the desk never saw this candidate at all". Recorded
        even when a retry later recovers the symbol, and NOT un-recorded when
        it does: that case logs at INFO, leaves `data_status["tech"]` reading
        "ok", and would otherwise be completely invisible to the operator
        while still costing a paid LLM round-trip. Since 2026-09-23 the risk
        stage reconciles these entries against the book it holds and reports a
        recovered one as the `analysis_parse_loss_recovered` COST note instead
        of as missing coverage (`_reconcile_parse_loss` in
        `src/pipeline_stages.py`), so the signal this counter exists to give
        survives without the counter ever being erased.
        """
        if self._suspended:
            return
        with self._lock:
            self._drops[(model_name, key)] += 1

    def snapshot(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._counts)

    def dropped_snapshot(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._drops)

    def total_null_coercions(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def total_dropped(self) -> int:
        with self._lock:
            return sum(self._drops.values())

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._drops.clear()

    def describe_null_coercions(self) -> str:
        """One-line, grep-able summary for the operator log / RM advisory."""
        snap = self.snapshot()
        if not snap:
            return ""
        return ", ".join(
            f"{model}.{field}x{n}"
            for (model, field), n in sorted(snap.items(), key=lambda kv: -kv[1])
        )

    def describe_dropped(self) -> str:
        snap = self.dropped_snapshot()
        if not snap:
            return ""
        return ", ".join(
            f"{model}:{key}" + (f"x{n}" if n > 1 else "")
            for (model, key), n in sorted(snap.items(), key=lambda kv: -kv[1])
        )


parse_telemetry = AnalysisParseTelemetry()

# Recordable "don't know" for a soft-exit field (`thesis_invalid_if` /
# `catalyst`) when the model sent JSON null on an actionable call. Not a
# falsifier and not a catalyst — `check_thesis_invalid_if` treats it as
# UNPARSEABLE, the same as empty. Empty remains the correct value on a
# neutral Tech read (the prompt says leave it empty). Distinct from silent
# schema-default "" so Risk can see "the seat said it does not know"
# instead of "the field was wiped".
SOFT_EXIT_UNKNOWN = "unknown"
_SOFT_EXIT_FIELDS = frozenset({"thesis_invalid_if", "catalyst"})
# Durable refuse-before-book reason after mechanical heal + one paid retry
# still left an open name without a real "I'll sell if". Not invented
# prose and not a catalyst. The #432 isolate-unknown-only gate is the
# TEMPORARY last-resort that records this reason; delete that isolate
# when a live session proves no actionable name arrives blank.
SOFT_EXIT_MISSING_AFTER_RETRY = "soft-exit missing after retry"
ACTIONABLE_TECH_RATINGS = frozenset({"buy", "strong_buy", "sell", "strong_sell"})


def stated_soft_exit(value: str | None) -> str:
    """A checkable falsifier/catalyst, or empty.

    `unknown` is the recordable don't-know token, not a condition. Callers
    that need a checkable string (hard-stop substitution, constructor
    parenthetical) treat it as absent. The raw field stays `unknown` so
    Risk can see the seat said it does not know.
    """
    text = (value or "").strip()
    if not text or text.lower() == SOFT_EXIT_UNKNOWN:
        return ""
    return text


def soft_exit_unknown_after_heal(value: str | None) -> bool:
    """True when heal left the recordable don't-know token, not a falsifier.

    Distinct from omitted empty (neutral Tech, legacy constructors). A
    BUY/SHORT still carrying this token is refused by name before Risk —
    never filled with invented thesis/catalyst text, never used to veto
    the rest of the plan.
    """
    return (value or "").strip().lower() == SOFT_EXIT_UNKNOWN


def missing_stated_falsifier(value: str | None) -> bool:
    """True when there is no checkable 'I'll sell if' string.

    Empty, whitespace, and the recordable don't-know token `unknown` are
    all missing. Neutral Tech may omit; an actionable rating and an
    open/increase target may not enter the ticket book in this state.
    A reduction or close may omit the field — that omit does not make
    PM thesis free text a sell warrant.
    """
    return not stated_soft_exit(value)


def open_target_missing_falsifier(target, *, intent: str | None = None) -> bool:
    """Open/increase target with no real thesis_invalid_if.

    Falsifier is required only for opens and increases. Reductions and
    closes may omit it; a blank-falsifier size-down is not a soft-exit
    — the constructor may only build SELL/COVER when a mechanical
    size-down vs the live book is checkable, and that warrant is the
    named trigger. PM thesis free text explains; it cannot create the
    sell. Never invents a string. Catalyst is not this check — it
    stays optional except the dated unmeasurable-range exception
    already gated in Python.

    `intent` is `"buy"` / `"short"` / `"sell"` from
    `PortfolioManagerAgent._target_intent` (current size/risk vs the
    proposed target). A `"sell"` — a trim to a lower non-zero size, or
    a full close — is exempt. When `intent` is omitted, only a full
    close (`is_close`) is exempt: callers that can classify from the
    live book must pass intent so a non-zero trim is not treated as an
    open.
    """
    if target is None:
        return False
    if intent is not None:
        if intent not in ("buy", "short"):
            return False
        return missing_stated_falsifier(getattr(target, "thesis_invalid_if", None))
    is_close = getattr(target, "is_close", None)
    if callable(is_close):
        if target.is_close:
            return False
    elif is_close:
        return False
    return missing_stated_falsifier(getattr(target, "thesis_invalid_if", None))


# Defaulted fields where an explicit null must STILL reject the object.
#
# The general rule above is safe because a default of "", [], "unknown" or
# None means "nothing was said". These two defaults are not that: they are
# affirmative instructions that move capital, and they carry defaults only for
# backward-compatible replay of historical rows, not because absence is
# semantically harmless.
#
#   TargetPosition.direction   default "long" is a SIDE. Coercing a null here
#                              would silently turn a short into a long.
#   RiskVerdict.scale_all_buys default 1.0 is "apply no risk reduction".
#                              Coercing a null would silently release a brake
#                              the Risk Manager may have meant to pull.
#
# Both belong to objects that are dropped per-item by their callers, so the
# blast radius of keeping them strict is one target / one verdict, not a
# whole session.
_NULL_MUST_FAIL: frozenset[tuple[str, str]] = frozenset({
    ("TargetPosition", "direction"),
    ("RiskVerdict", "scale_all_buys"),
})


_NULL_TOLERANT_FIELDS_CACHE: dict[str, frozenset[str]] = {}
_NULL_TOLERANT_CACHE_LOCK = threading.Lock()


def _null_droppable_fields(cls: type[BaseModel]) -> frozenset[str]:
    """Field names on `cls` where an explicit null should mean "absent".

    A field qualifies when it (a) has a default, (b) does NOT already accept
    None, and (c) is not on `_NULL_MUST_FAIL`. Computed once per class —
    `TypeAdapter` construction is not cheap and this runs on every parsed
    object.
    """
    key = f"{cls.__module__}.{cls.__qualname__}"
    cached = _NULL_TOLERANT_FIELDS_CACHE.get(key)
    if cached is not None:
        return cached
    names: set[str] = set()
    for field_name, field in cls.model_fields.items():
        if field.is_required():
            continue
        if (cls.__name__, field_name) in _NULL_MUST_FAIL:
            continue
        try:
            TypeAdapter(field.annotation).validate_python(None)
        except Exception:
            names.add(field_name)   # rejects None *and* has a default
        else:
            continue                # already Optional — nothing to do
    result = frozenset(names)
    with _NULL_TOLERANT_CACHE_LOCK:
        _NULL_TOLERANT_FIELDS_CACHE[key] = result
    return result


# Every prompt in config/prompts/*.md instructing the `[UNSOURCED:<reason>]`
# token documents it as a value for a MISSING QUANTITATIVE/TEXT fact — the
# token is a single string literal. A field typed as a list has no way to
# carry that literal: `EarningsRevenue.segments: list[EarningsSegment]`
# received the bare string `"[UNSOURCED:segment_data_not_disclosed]"` from
# gemini-2.5-flash-lite (2026-09), which is not a list, so pydantic raised
# `list_type` and the whole `EarningsAnalysis` was discarded — the model
# followed the token instruction literally into a field the prompt should
# never have pointed it at. Coerce it to `[]` instead of losing the object,
# on the same "kept, not silently blanked" discipline as `LLMOutputModel`'s
# null-coercion above.
_UNSOURCED_TOKEN_RE = re.compile(r"^\[UNSOURCED(?::[^\]]*)?\]$")

_LIST_TYPED_FIELDS_CACHE: dict[str, frozenset[str]] = {}
_LIST_TYPED_FIELDS_CACHE_LOCK = threading.Lock()


def _list_typed_fields(cls: type[BaseModel]) -> frozenset[str]:
    """Field names on `cls` declared as `list[...]`. Cached per class."""
    key = f"{cls.__module__}.{cls.__qualname__}"
    cached = _LIST_TYPED_FIELDS_CACHE.get(key)
    if cached is not None:
        return cached
    names = {
        field_name
        for field_name, field in cls.model_fields.items()
        if get_origin(field.annotation) is list
    }
    result = frozenset(names)
    with _LIST_TYPED_FIELDS_CACHE_LOCK:
        _LIST_TYPED_FIELDS_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# `SkipJsonSchema` — the marker used throughout this file for a field that
# exists on a parsed-from-LLM model for the DESK's own bookkeeping and must
# never appear on the surface the model actually sees.
#
# `BaseAgent._response_format_for` builds the OpenRouter / OpenAI
# `response_format` from `result_model.model_json_schema()`. On the strict
# path `_strictify_schema` then forces EVERY property in that schema to be
# `required`, so a desk-owned field left in it is a field the model is
# COMPELLED to invent and whose value the pipeline overwrites the instant it
# parses the response. On the `strict: False` fallback path — taken when a
# model carries a free-form map, e.g. `NewsIntelligenceReport` — the field is
# an invitation rather than a compulsion, which is weaker but still wrong: it
# is an output slot for something the seat is not being asked.
#
# Annotating the field removes it (and any `$defs` reachable only through it)
# from the rendered schema. Validation, assignment, storage and serialisation
# are completely unchanged: the desk still sets it, still persists it, still
# reads it back. Putting the marker on the field itself — rather than
# maintaining a second, model-facing copy of the class — is what stops the
# stored shape and the sent shape from drifting apart.
#
# Use it ONLY for a field the desk itself fills. A field the model is
# genuinely being asked for stays visible. `tests/
# test_response_format_desk_only_fields.py` is the mechanical check.
# ---------------------------------------------------------------------------


class LLMOutputModel(BaseModel):
    """Base for every model parsed out of an LLM response.

    Carries one behaviour: an explicit `null` on a field that declares a
    default is treated as an absent key, tallied in `parse_telemetry`, and
    logged. See the block comment above for the measurement and the
    reasoning.

    Models that are NOT parsed from LLM output (OHLCV, Position,
    TechnicalIndicators, AgentLog, MissedOpportunitySnapshot) deliberately do
    not inherit this: a null in those comes from our own code, and a loud
    failure is the correct response to our own bug.
    """

    @model_validator(mode="before")
    @classmethod
    def _explicit_null_means_absent(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        droppable = _null_droppable_fields(cls)
        if not droppable:
            return values
        hits: list[str] = []
        for field_name in droppable:
            # 2026-09-03: an empty string is the same "the model said nothing"
            # signal as an explicit null for these fields — evening_analyst's
            # `theme_durability` (Literal[...] = "unknown") was observed in
            # production emitting `""` instead of `null` when left unfilled,
            # which this validator's original None-only check did not catch,
            # so it fell through to Literal validation and got the whole
            # entry dropped. Comparing to "" is safe for every droppable
            # field: droppable fields are exactly the ones with a default
            # that itself rejects None, so on a str-typed field an "" hit
            # either matches the field's own default (no behavior change,
            # e.g. `universe_addition_reason`) or coerces to the declared
            # non-empty default (the fix, e.g. `theme_durability`).
            v = values.get(field_name, ...)
            if v is None or v == "":
                hits.append(field_name)
        if not hits:
            return values
        original = dict(values)
        values = dict(values)
        tallied: list[str] = []
        rating = str(values.get("rating") or original.get("rating") or "").strip().lower()
        for field_name in sorted(hits):
            # 2026-09-11: deleting the key is what makes the declared default
            # apply — but ONLY on the construction path. Every model here
            # that enables `validate_assignment` re-runs this validator on
            # assignment, and pydantic treats the dict it gets back as the
            # instance's COMPLETE new state: a key deleted there leaves the
            # field genuinely UNSET, so the next plain read of it raises
            # `AttributeError`, not the default. Measured on `TargetPosition`
            # — capping `risk_allocation_pct` deleted `thesis_invalid_if` and
            # `catalyst` (both default `""`), and
            # `PortfolioConstructor._build_buy` then raised reading
            # `target.thesis_invalid_if`. It was latent only because the
            # sub-floor catalyst gate's own cap was the main assignment site
            # and the constructor refused those orders earlier for an
            # unrelated reason.
            #
            # So: when the value ALREADY EQUALS the declared default,
            # deleting it changes nothing on construction (this validator's
            # own docstring above says exactly that) and breaks the instance
            # on assignment. Leave it in place.
            default = cls.model_fields[field_name].get_default(
                call_default_factory=True,
            )
            incoming = original.get(field_name, ...)
            # A stated non-empty soft-exit must survive a later null wipe.
            # Never invent a falsifier/catalyst string — only keep what
            # the model already wrote.
            if (
                field_name in _SOFT_EXIT_FIELDS
                and isinstance(incoming, str)
                and incoming.strip()
                and incoming.strip().lower() != SOFT_EXIT_UNKNOWN
            ):
                values[field_name] = incoming
                continue
            # Empty string that already equals the declared default is the
            # schema working (neutral Tech leaves thesis_invalid_if empty),
            # not a drop. Tallying it produced ~200k journal lines on
            # 2026-09-16 and fed Risk a false "fields were nulled" integrity
            # reject. Do not count it.
            if incoming == "" and incoming == default:
                continue
            # Explicit null on an actionable soft-exit: record "unknown"
            # so "don't know" is distinct from omitted empty. Neutral Tech
            # still lands on empty — the prompt says leave it empty.
            if field_name in _SOFT_EXIT_FIELDS and incoming is None:
                actionable = cls.__name__ == "TargetPosition" or (
                    rating not in ("", "neutral")
                )
                if actionable:
                    values[field_name] = SOFT_EXIT_UNKNOWN
                    parse_telemetry.record_null_coercion(cls.__name__, field_name)
                    tallied.append(field_name)
                    continue
                if values.get(field_name) != default:
                    del values[field_name]
                continue
            if values[field_name] != default:
                del values[field_name]
            parse_telemetry.record_null_coercion(cls.__name__, field_name)
            tallied.append(field_name)
        if tallied:
            logger.warning(
                "%s: dropped explicit null/empty on defaulted field(s) %s — the "
                "object is kept and the declared default applies, but the model "
                "said nothing where the prompt asked for something",
                cls.__name__, ", ".join(sorted(set(tallied))),
            )
        # Mechanical heal (owner 2026-09-16): if a stated non-empty
        # thesis_invalid_if / catalyst survived on the original dict and a
        # later drop blanked the canonical field, put the stated string
        # back. Never invents a falsifier. Lazy import: seat_heal imports
        # sector maps from this module.
        try:
            from src.seat_heal import restore_stated_soft_exits
            values, _restored = restore_stated_soft_exits(values, original)
        except Exception:
            pass
        return values

    @model_validator(mode="before")
    @classmethod
    def _unsourced_token_on_list_field_means_empty(cls, values: Any) -> Any:
        """A bare `[UNSOURCED:<reason>]` string on a `list[...]` field is
        coerced to `[]` instead of failing the whole object.

        See the block comment above `_UNSOURCED_TOKEN_RE` for why this
        happens: the token is documented prompt-wide as the way to mark a
        missing value, but it is a string literal and some fields it gets
        pointed at (e.g. `EarningsRevenue.segments`) are lists. Kept, not
        silently blanked — tallied through the same telemetry as the null
        coercion above so the gap is visible to the operator.
        """
        if not isinstance(values, dict):
            return values
        list_fields = _list_typed_fields(cls)
        if not list_fields:
            return values
        hits: list[str] = []
        for field_name in list_fields:
            v = values.get(field_name, ...)
            if isinstance(v, str) and _UNSOURCED_TOKEN_RE.match(v.strip()):
                hits.append(field_name)
        if not hits:
            return values
        values = dict(values)
        for field_name in sorted(hits):
            values[field_name] = []
            parse_telemetry.record_null_coercion(cls.__name__, field_name)
        logger.warning(
            "%s: coerced [UNSOURCED:...] token on list field(s) %s to [] — "
            "the model wrote the missing-data token into a field declared "
            "as a list; the object is kept, the gap is logged",
            cls.__name__, ", ".join(sorted(hits)),
        )
        return values


class Nomination(LLMOutputModel):
    """A research seat's request that Technical examine a candidate.

    Phase 9 (`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2): before this,
    Technical was the ONLY seat that could originate a trade idea — every
    other seat could only rate a symbol Technical had already picked. A
    nomination inverts that: any seat can ask the desk to look at a
    symbol, and an on-demand Technical call decides whether there is an
    actual tradeable setup.

    This is deliberately NOT a trade recommendation. It carries the
    minimum a responder pass needs to act on it: which symbol, how
    strongly the nominating seat feels, and the concrete observation
    behind the ask — a nomination with no stated reason is not a
    nomination, hence `observation` is required non-empty.

    `seat` is stamped by the pipeline when a report's nominations are
    collected (`src/pipeline_stages.py::_collect_seat_nominations`), not
    emitted by the LLM — a seat's own prompt never has to know its own
    internal name, only that it may nominate. It defaults to "" so a
    directly-constructed Nomination (e.g. in a test) doesn't require it.
    """
    symbol: str
    seat: str = ""
    conviction: Literal["low", "medium", "high"]
    observation: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("observation")
    @classmethod
    def require_observation(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("nomination observation cannot be empty")
        return text

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(values, lower_fields=("conviction",))


def _sanitize_nominations_field(values):
    """Drop malformed nomination entries rather than fail the whole report.

    Mirrors `MacroAnalysis._sanitize_sector_guidance`: one bad nomination
    (empty observation, bad conviction, empty symbol) must not cost the
    seat its entire structured output for the run — the rest of the
    analysis is real and valuable even when the model's nomination
    attempt was malformed. Applied as a `mode="before"` validator on each
    nominating seat's report model.
    """
    if not isinstance(values, dict):
        return values
    raw = values.get("nominations")
    if not isinstance(raw, list):
        return values
    cleaned = []
    for item in raw:
        # Already a validated Nomination — the direct-construction path
        # (`MacroAnalysis(..., nominations=[Nomination(...)])`, used by
        # tests and any programmatic caller) hands this validator real
        # model instances, not dicts. Pass those straight through; only
        # dict items (the LLM-JSON path) need re-validation.
        if isinstance(item, Nomination):
            cleaned.append(item)
            continue
        if not isinstance(item, dict):
            continue
        try:
            Nomination.model_validate(item)
        except Exception:
            continue
        cleaned.append(item)
    values["nominations"] = cleaned
    return values


class OHLCV(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class TechnicalIndicators(BaseModel):
    symbol: str
    ma_20: float | None = None
    ma_50: float | None = None
    ma_200: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    atr_14: float | None = None
    volume_change_pct: float | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

class VerdictEvidence(BaseModel):
    """One checkable fact backing an `AnalystVerdict`.

    Phase 13 (`docs/QAMC_REMEDIATION_SPEC.md` §13.2, item 3): evidence is
    "specific, checkable facts backing the call (a number, a dated event, a
    level) — not prose alone". So an item is a labelled NUMBER, a labelled
    DATED event, or a labelled observation, and an item with none of those
    is refused — a bare label is a heading, not evidence.

    Not an `LLMOutputModel`: in this first increment every verdict is
    DERIVED in Python from a seat's already-validated report (see
    `TechAnalysisResult.to_verdict`), never parsed from an LLM response. A
    null here would be our own bug and should fail loudly.
    """
    label: str = Field(min_length=1)          # e.g. "stop_loss", "trend", "risk_reward"
    value: float | None = None                # a price, a level, a ratio
    as_of: date | None = None                 # a dated event
    text: str = ""                            # the observation, when it is not a number

    @model_validator(mode="after")
    def _require_something_checkable(self):
        if self.value is None and self.as_of is None and not self.text.strip():
            raise ValueError(
                f"evidence {self.label!r} carries no value, no date and no "
                "text — a label alone is not a checkable fact"
            )
        return self


class AnalystVerdict(BaseModel):
    """The one shape every specialist seat hands the Portfolio Manager.

    Phase 13 (`docs/QAMC_REMEDIATION_SPEC.md` §13.2): the PM was comparing
    several different essays and picking one. This is the checkable,
    comparable judgement instead — the same four things from every seat:

    1. **direction** — bullish / bearish / neutral, PLUS `magnitude`, how
       far in that direction the seat leans on a 0..1 scale. The label
       vocabulary is the one `stance_is_aligned` and the evidence registry
       already speak (`StockNewsItem.sentiment`, `SmartMoneyFinding.stance`,
       `MacroAnalysis.equity_outlook`), so a verdict can be netted against
       the §9.4 score without a translation table.
    2. **conviction** — how sure the seat is, on the desk's existing
       `high` / `medium` / `low` scale, SEPARATE from what it thinks.
    3. **evidence** — a list of `VerdictEvidence`, at least one for any
       directional call. A neutral read may carry none.
    4. **invalidation** — the stated condition under which the call is
       wrong. Required non-empty for any directional call. A neutral verdict
       is the ABSENCE of a call, so there is nothing to falsify and the field
       may be blank; a neutral verdict with a non-zero magnitude is refused
       as self-contradictory.

    `seat` and `symbol` are identity, not judgement. `seat` uses the
    evidence-registry key for the seat ("technical", "news", ...) so a
    verdict and a registry stance about the same name agree on who said it.

    `signed_magnitude` is the number a ranking or a netting rule reads:
    +magnitude for bullish, -magnitude for bearish, 0 for neutral.

    Not an `LLMOutputModel` — see `VerdictEvidence` for why.
    """
    seat: str = Field(min_length=1)
    symbol: str
    direction: Literal["bullish", "bearish", "neutral"]
    magnitude: float = Field(ge=0.0, le=1.0)
    conviction: Literal["high", "medium", "low"]
    evidence: list[VerdictEvidence] = Field(default_factory=list)
    invalidation: str = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("invalidation", mode="before")
    @classmethod
    def _strip_invalidation(cls, value):
        return "" if value is None else str(value).strip()

    @model_validator(mode="after")
    def _a_call_must_be_falsifiable_and_backed(self):
        if self.direction == "neutral":
            if self.magnitude != 0.0:
                raise ValueError(
                    f"{self.symbol}: a neutral verdict cannot carry magnitude "
                    f"{self.magnitude} — neutral means no lean"
                )
            return self
        if not self.invalidation:
            raise ValueError(
                f"{self.symbol}: a {self.direction} verdict from {self.seat} "
                "must state its invalidation condition — a call nobody can "
                "prove wrong is not a call"
            )
        if not self.evidence:
            raise ValueError(
                f"{self.symbol}: a {self.direction} verdict from {self.seat} "
                "must cite at least one piece of checkable evidence"
            )
        return self

    @computed_field
    @property
    def signed_magnitude(self) -> float:
        if self.direction == "bullish":
            return self.magnitude
        if self.direction == "bearish":
            return -self.magnitude
        return 0.0


#: How a Technical rating maps onto `AnalystVerdict.magnitude`. The rating
#: scale has exactly two directional rungs a side (buy / strong_buy), so this
#: is an EQUAL-SPACING ordinal encoding of the desk's own scale — the same
#: posture as `CONVICTION_SCORE` in `ops/model_policy/deterministic_selection`
#: reading band tops — not a tuned weight. Nothing has been measured that
#: would justify any other spacing (Phase 13 §13.3: start equal, adjust only
#: on out-of-sample proof).
RATING_MAGNITUDE: dict[str, float] = {
    "strong_buy": 1.0, "buy": 0.5, "neutral": 0.0, "sell": 0.5, "strong_sell": 1.0,
}

#: `AnalystVerdict.magnitude` for a DIRECTIONAL verdict from a seat that
#: states no independent strength of its own. The same "ordinary conviction"
#: rung `RATING_MAGNITUDE` gives Technical's single-strength buy/sell, reused
#: rather than respelled, so there is one number here and not four.
#:
#: WHY THIS IS FLAT, 2026-09-13 (see `docs/INCIDENT_HISTORY.md`, retired
#: item 31). `score_verdict`
#: (`src/verdicts.py`) is `magnitude + conviction_score(conviction)` — TWO
#: signals, weight 1 each. Three seats used to derive `magnitude` from the
#: very field they also hand to `conviction`:
#:
#:   * news       — magnitude was a table on `conviction` itself;
#:   * macro      — magnitude was a table on `confidence`, which IS the
#:                  conviction it reports;
#:   * smart_money— magnitude AND conviction were both tables on the single
#:                  `economic_role` label.
#:
#: For those seats the composite was not two signals averaged, it was one
#: signal counted twice, at a spacing (0.33/0.67/1.0 for news, 0.25/0.5/0.75
#: for macro, 1.0/0.6/0.3 for smart_money) that no source and no measurement
#: stood behind — three different unsourced encodings of the same 3-rung
#: scale. A "actionable" smart-money label alone scored 2.0, the maximum the
#: composite can produce, equal to a strong_buy at high conviction from the
#: one seat that was measured (item 18) as actually concluding.
#:
#: The rule now: magnitude carries information ONLY where a seat states a
#: strength independent of its confidence. Technical does (its rating rungs).
#: Nothing else on this desk does, so nothing else claims one. This REMOVES
#: invented numbers rather than replacing them with better-argued ones —
#: `qamc-no-arbitrary-numbers-principle`. The dropped inputs are not lost to
#: the reader: macro's `regime_shift`/`shift_reason` still reach the verdict
#: as evidence and invalidation, and smart_money's `economic_role` still sets
#: conviction via `_SMART_MONEY_ROLE_CONVICTION`, whose ordering is a
#: restatement of the pre-existing `_ROLE_RANK`, not a new judgment.
#:
#: AND WHY IT IS ZERO, not 0.5 — corrected 2026-09-13, same day, on
#: adversarial review before merge. The first version of this deletion set the
#: four rungless seats to 0.5, "Technical's `buy` rung, reused rather than
#: respelled". Borrowing is not deriving: 0.5 is a number read off ANOTHER
#: seat's scale, and these four seats do not have that scale — that is the
#: whole reason they are here. A seat that states no strength states no
#: strength, and the honest encoding of "no distance claimed" is no distance.
#:
#: This is only coherent because `rank_verdicts` no longer AVERAGES seats (see
#: `src/verdicts.py`): such a seat still contributes its own weighted
#: conviction to the total, so a directional read with nothing behind it is
#: not silently equal to no coverage at all — it is equal to exactly what it
#: is worth, its conviction. Under the old weighted average a zero here would
#: have DRAGGED an agreeing candidate down; under a sum it cannot.
#:
#: If a seat is ever given a real strength scale of its own — measured, or
#: read from the instrument the way Technical's rungs are — that is a schema
#: change to RATIFY with the derivation attached, not a constant to restore
#: here. Tracked as `docs/WORK.md` item 62.
NO_STATED_STRENGTH: float = 0.0

RATING_DIRECTION: dict[str, str] = {
    "strong_buy": "bullish", "buy": "bullish", "neutral": "neutral",
    "sell": "bearish", "strong_sell": "bearish",
}


class TechReasoningChain(LLMOutputModel):
    """5-step CoT for a single symbol — forces the LLM to show its work per
    framework step. Every field has `min_length=1` so the LLM cannot skip a
    step by sending an empty string. This matches the discipline already in
    place on the other CoT chains (Evening / Position / Meta) and closes
    the audit gap that contradicted the README's 'schema-enforced CoT,
    LLM cannot skip steps' claim.
    """
    trend: str = Field(min_length=1)                 # MA alignment, price vs MA20/50/200
    momentum: str = Field(min_length=1)              # RSI level, MACD cross direction
    volatility: str = Field(min_length=1)            # BB position, ATR expansion/contraction
    volume: str = Field(min_length=1)                # volume confirming or diverging vs trend
    support_resistance: str = Field(min_length=1)    # key levels from indicators + recent pivots


class TechAnalysisResult(LLMOutputModel):
    symbol: str
    rating: Literal["strong_buy", "buy", "neutral", "sell", "strong_sell"]
    conviction: Literal["high", "medium", "low"] = "medium"
    entry_price: float | None = None
    reference_target: float | None = None  # renamed from exit_price — it's a soft reference, not a hard TP
    stop_loss: float | None = None
    # --- Structural levels (2026-08-27) -------------------------------------
    # The prior design let the analyst omit levels and had
    # `PortfolioConstructor` invent replacements: `entry - 2*ATR` for the stop
    # and `entry * (1 + 2*stop_gap)` for the target. Every downstream metric
    # — thesis_progress, pace, R/R, TARGET_BREACH — was then measured against
    # a number nobody derived from the chart. These fields make the levels
    # first-class so the invented fallbacks can be deleted.
    #
    # Prices only; no indicator values. Empty lists are legal for `neutral`
    # (no trade is being proposed) but not for an actionable rating.
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    # PYTHON-SET, not LLM-emitted (same pattern as `atr_14` below): every
    # level `src/data/levels.py::find_structural_levels` found over the full
    # fetched history, supports and resistances unioned into one list of bare
    # prices. The two fields above are the LLM's SELECTION from the levels
    # block in its prompt; this is the block itself, preserved so the
    # constructor can derive the target arithmetically instead of reading the
    # model's `reference_target` (2026-09-01 — see the target-derivation
    # section of src/data/levels.py for why that division was invalid).
    #
    # Unioned on purpose. `find_structural_levels` calls a level support or
    # resistance relative to the LAST CLOSE; the trade is entered at a live
    # price that can sit on the other side of it, so the partition is redone
    # against the actual entry at derivation time.
    computed_levels: list[float] = Field(default_factory=list)
    # PYTHON-SET, keyed by the same prices as `computed_levels` above: how
    # many pivots `find_structural_levels` clustered into each one (Phase
    # 12.1, 2026-09-03). `computed_levels` collapses every qualifying level
    # to a bare price, which is enough to derive a target but not enough to
    # ask "how much do we trust this specific level" — and §12.1 made that
    # question load-bearing by honouring a level-backed stop however tight.
    # `PortfolioConstructor._level_backing_stop` reads this to enforce
    # `risk.min_level_touches_for_stop_honor`; nothing else consumes it, and
    # target derivation is deliberately untouched by this field — see
    # docs/RESEARCH_FINDINGS.md §7 for the touch-count evidence and the
    # threshold derived from it.
    computed_level_touches: dict[float, int] = Field(default_factory=dict)
    # PYTHON-SET (2026-09-12): what the bar history behind `computed_levels`
    # was — one of the COVERAGE_* states in `src/data/levels.py`. An empty
    # `computed_levels` with coverage "measured" is a chart with no
    # structure (a trade refusal); with any other coverage it is a DATA
    # fault (unusable history) that must be recorded and alerted as such,
    # never counted as a trade the desk judged. Defaults to "unknown",
    # which the derivation classifies with the faults, fail-closed.
    levels_coverage: str = "unknown"
    # PYTHON-SET (2026-09-12, docs/WORK.md item 54), same pattern as the
    # two above, from the same bars: the last completed bar's low and high
    # — the SIGNAL bar the analyst judged — and how many completed sessions
    # the desk actually had for this instrument. The constructor reads the
    # bar edge as the instrument's own fallback stop when nothing computed
    # backs the typed one (the wider of it and the ATR noise band —
    # Kullamägi's "low of the day"), and the bar count to refuse a listing
    # too young to measure (`src/data/technical.py::
    # LONGEST_INDICATOR_WINDOW`). None = not recorded (older persisted row,
    # hand-built object): the band alone decides, and no youth is assumed.
    signal_bar_low: float | None = None
    signal_bar_high: float | None = None
    bars_available: int | None = None
    # How the position must be MANAGED, decided at entry from the chart:
    #   "range"    — clear structure on both sides. Fixed target is meaningful;
    #                thesis_progress and pace are valid measurements.
    #   "breakout" — no overhead structure (highs, clean break). The target is
    #                a MEASURED MOVE reference, not a level anyone is defending;
    #                the position is managed by trailing and progress/pace must
    #                be disabled downstream (see QAMC_REMEDIATION_SPEC Phase 3).
    setup_type: Literal["range", "breakout"] | None = None
    # The analyst's own estimate of how many trading sessions this thesis needs
    # to resolve. Pinned at entry and never recomputed. This replaces the
    # self-referential `avg_hold_days` calibration that made `pace` a feedback
    # loop: selling quickly shrank the average, which made every position look
    # stalled, which drove more selling.
    expected_horizon_sessions: int | None = None
    reasoning_chain: TechReasoningChain
    reasoning: str  # 1-sentence summary; reasoning_chain carries the full analysis
    # Soft exit signal separate from the hard stop_loss. Example:
    # "MACD histogram turns negative for 2 consecutive closes" — lets PM / midday
    # exit BEFORE the broker stop fires, saving the 3-5% typically given up
    # between thesis-break and stop-trigger.
    # MEASURED 2026-09-01: models emit `"thesis_invalid_if": null` on about 2% of
    # candidates (42 explicit nulls in 2,056 field occurrences across two weeks
    # of production responses, most recently the morning of 2026-09-01). Every
    # OTHER field they null here is typed `| None` and tolerates it; this one
    # was a bare `str`, so pydantic rejected the null and the WHOLE candidate
    # was dropped with "Failed to parse tech analysis item". A silently
    # discarded analysis is an idea the desk never gets to consider, which is
    # the under-deployment problem arriving by a side door. Found by the
    # rehearsal rig, confirmed against the production database.
    thesis_invalid_if: str = ""

    @field_validator("thesis_invalid_if", mode="before")
    @classmethod
    def _null_thesis_invalid_if_is_blank(cls, v, info: ValidationInfo):
        """An absent soft-exit signal is blank, never a reason to bin the read.

        A stated non-empty string is never replaced. Explicit null on an
        actionable rating records `unknown` (not a falsifier). Neutral
        stays empty, matching the prompt.
        """
        if isinstance(v, str) and v.strip():
            return v
        rating = str((info.data or {}).get("rating") or "").strip().lower()
        if v is None and rating not in ("", "neutral"):
            return SOFT_EXIT_UNKNOWN
        return "" if v is None or v == "" else v

    # Days since this rating was first issued (unchanged). Python-computed from
    # TechStore after TechAnalystAgent returns; None on first run or when the
    # symbol wasn't in yesterday's cache. Fresh=1 means "new today", 7+=stale.
    signal_age_days: int | None = None
    # ATR(14) carried through from the input indicators (Python-set after
    # TechAnalystAgent returns — the LLM doesn't emit this; it's read from
    # the indicators object the prompt was built from). Used downstream by
    # `PortfolioConstructor._resolve_stop` as a volatility-aware fallback
    # when neither the target's `suggested_stop_price` nor the LLM's
    # `stop_loss` is available — `entry - 2*ATR` thrashes less on
    # high-volatility names than a hardcoded 5% stop.
    atr_14: float | None = None

    @computed_field
    @property
    def risk_reward(self) -> float | None:
        """Reward/risk ratio from entry, stop, and reference_target.

        Computed in Python (not trusted to the LLM). For BUY we expect (target > entry > stop);
        for SELL the inequalities flip. Returns None when any price is missing, the rating
        is neutral, or the geometry is malformed (so PM / RM won't render a fake ratio).

        **This is the ANALYST's geometry, not the order's**, and the two
        are routinely different: `entry_price` is the analyst's snapshot
        price and `reference_target` is the model's guess, while the order
        ships at the live price against a target the constructor derives
        from the bars. `TradeDecision.reward_risk` is the number the desk
        gates on. Both now divide through the same `reward_to_risk`, so
        any gap between them is a genuine difference of INPUTS — which is
        what the constructor's reconciliation note explains — and never a
        difference of arithmetic.
        """
        if self.rating in ("buy", "strong_buy"):
            is_short = False
        elif self.rating in ("sell", "strong_sell"):
            is_short = True
        else:
            return None
        ratio = reward_to_risk(
            self.entry_price, self.stop_loss, self.reference_target,
            is_short=is_short,
        )
        return None if ratio is None else round(ratio, 2)

    def to_verdict(self) -> "AnalystVerdict":
        """This read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion: every field is read off values
        this result already carries and the analyst already validated. The
        technical seat was measured (item 18, 2026-09-03) as the one seat
        that already CONCLUDES — rating, conviction, R/R, levels, `Invalid
        if`, one-line why — so this is a mapping, not new prompting.

        direction / magnitude — `RATING_DIRECTION` / `RATING_MAGNITUDE`.
        conviction            — verbatim.
        evidence              — the numbers the desk can check against a
                                chart (entry, stop, target, R/R, the levels
                                the analyst selected) plus the five
                                reasoning-chain observations, labelled.
        invalidation          — `thesis_invalid_if` when the analyst stated
                                one. When it did not (measured ~2% of
                                actionable reads, see the field's own
                                comment), the hard stop IS the analyst's own
                                stated falsifier, so it is used, and the text
                                says so — nothing is invented.
        """
        direction = RATING_DIRECTION[self.rating]
        evidence: list[VerdictEvidence] = []
        for label, value in (
            ("entry_price", self.entry_price),
            ("stop_loss", self.stop_loss),
            ("reference_target", self.reference_target),
            ("risk_reward", self.risk_reward),
        ):
            if value is not None:
                evidence.append(VerdictEvidence(label=label, value=float(value)))
        for level in self.support_levels:
            evidence.append(VerdictEvidence(label="support_level", value=float(level)))
        for level in self.resistance_levels:
            evidence.append(VerdictEvidence(label="resistance_level", value=float(level)))
        # `computed_level_touches` (Python-set, `find_structural_levels`) is
        # already how many prior pivots back EACH computed level — used
        # elsewhere (`PortfolioConstructor._level_backing_stop`,
        # `risk.min_level_touches_for_stop_honor`) to decide whether a stop
        # earns the tight-stop exemption.
        #
        # RISK-SIDE ONLY, not every computed level. `computed_levels` is
        # `find_structural_levels`' supports and resistances UNIONED
        # (`TechAnalysisResult.computed_levels`'s own field comment), so a
        # blind sum over `computed_level_touches` counts overhead
        # resistance the same as underlying support — for a long, a
        # heavily-touched ceiling is supply IN THE WAY of the trade, not
        # evidence for it, and summing it in would score exactly the wrong
        # direction. This filters to the levels on the RISK side of the
        # analyst's own entry (below entry for a long, above it for a
        # short) — the same side `_level_backing_stop` looks at, and the
        # ONLY side `reward_risk_floor_applies` says a breakout is approved
        # on at all (`src/risk/constants.py`): "a real level-backed or
        # ATR-derived stop, plus the multi-agent conviction and evidence
        # checks". A breakout's overhead is deliberately not measured
        # anywhere in this desk's approval logic; this does not reach in
        # and measure it either.
        #
        # Summed across qualifying levels (not averaged: more
        # independently-touched structure under the stop is more evidence
        # behind it, not a dilution of any one level — same "evidence adds"
        # posture `score_verdict` already takes), attached as its own
        # evidence item so `src/verdicts.py::rank_verdicts` can read it as
        # a real ranking signal WITHOUT recomputing anything (WORK.md item
        # 141: this is what closes the residual alphabetical fallback left
        # when a tied composite tier has no risk_reward at all, e.g. an
        # all-breakout tier). No entry price, or no computed levels at
        # all, means zero — a real absence, not an invented value.
        stop_side_level_touches = 0.0
        if self.entry_price is not None:
            is_short = self.rating in ("sell", "strong_sell")
            for price, touches in self.computed_level_touches.items():
                on_risk_side = (
                    price >= self.entry_price if is_short else price <= self.entry_price
                )
                if on_risk_side:
                    stop_side_level_touches += touches
        evidence.append(VerdictEvidence(
            label="stop_side_level_touches", value=float(stop_side_level_touches),
        ))
        chain = self.reasoning_chain
        for label in ("trend", "momentum", "volatility", "volume", "support_resistance"):
            text = getattr(chain, label, "") or ""
            if text.strip():
                evidence.append(VerdictEvidence(label=label, text=text.strip()))

        invalidation = stated_soft_exit(self.thesis_invalid_if)
        if not invalidation and direction != "neutral" and self.stop_loss is not None:
            side = "below" if direction == "bullish" else "above"
            invalidation = (
                f"close {side} stop {self.stop_loss} (hard stop; the analyst "
                "stated no separate soft invalidation)"
            )
        return AnalystVerdict(
            seat="technical",
            symbol=self.symbol,
            direction=direction,
            magnitude=RATING_MAGNITUDE[self.rating],
            conviction=self.conviction,
            evidence=evidence,
            invalidation=invalidation,
        )

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values, lower_fields=("rating", "conviction"),
        )

    @model_validator(mode="after")
    def _validate_rating_price_consistency(self):
        """Enforce price fields match the rating's actionability.

        - Actionable (strong_buy, buy, sell, strong_sell): entry_price AND stop_loss required.
        - Stop must be on the protective side of entry (stop < entry for BUYs, stop > entry for SELLs).
        - Neutral: prices should be null; we don't hard-fail but clear them to avoid stale hints.
        """
        if self.rating == "neutral":
            # Coerce to None — PM's template would otherwise print stale numbers.
            self.__dict__["entry_price"] = None
            self.__dict__["reference_target"] = None
            self.__dict__["stop_loss"] = None
            self.__dict__["setup_type"] = None
            self.__dict__["expected_horizon_sessions"] = None
            return self

        if self.entry_price is None or self.entry_price <= 0:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires entry_price > 0"
            )
        if self.stop_loss is None or self.stop_loss <= 0:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires stop_loss > 0"
            )
        if self.rating in ("buy", "strong_buy"):
            if self.stop_loss >= self.entry_price:
                raise ValueError(
                    f"{self.symbol}: BUY stop_loss {self.stop_loss} must be below entry {self.entry_price}"
                )
        else:  # sell / strong_sell — stop (buy-back) must be above entry
            if self.stop_loss <= self.entry_price:
                raise ValueError(
                    f"{self.symbol}: SELL stop_loss {self.stop_loss} must be above entry {self.entry_price}"
                )

        # --- Structural requirements for actionable ratings (2026-08-27) ----
        # No levels, no trade. Previously the analyst could omit all of this and
        # PortfolioConstructor would invent a stop and a target; every downstream
        # measurement was then taken against numbers derived from nothing.
        if self.reference_target is None or self.reference_target <= 0:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires reference_target > 0 "
                f"(derive it from structure, or from a measured move on a breakout)"
            )
        if self.rating in ("buy", "strong_buy"):
            if self.reference_target <= self.entry_price:
                raise ValueError(
                    f"{self.symbol}: BUY reference_target {self.reference_target} "
                    f"must be above entry {self.entry_price}"
                )
        else:
            if self.reference_target >= self.entry_price:
                raise ValueError(
                    f"{self.symbol}: SELL reference_target {self.reference_target} "
                    f"must be below entry {self.entry_price}"
                )
        if self.setup_type is None:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires setup_type "
                f"('range' or 'breakout') — it determines how the exit is managed"
            )
        if self.expected_horizon_sessions is None or self.expected_horizon_sessions <= 0:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires "
                f"expected_horizon_sessions > 0 (pinned at entry; pace is measured against it)"
            )
        if not self.support_levels and not self.resistance_levels:
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires at least one "
                f"structural level (support_levels and/or resistance_levels)"
            )
        # Never-blank soft-exit (2026-09-17): an actionable rating without a
        # real "I'll sell if" is not a tradeable idea. Empty and `unknown`
        # are missing, not "the analyst had nothing to say". Neutrals may
        # omit — the prompt says leave it empty. Nothing here invents a
        # falsifier string; the chunk retry re-asks, then the name is
        # refused before the book.
        if (
            self.rating in ACTIONABLE_TECH_RATINGS
            and missing_stated_falsifier(self.thesis_invalid_if)
        ):
            raise ValueError(
                f"{self.symbol}: rating={self.rating} requires a real "
                f"non-empty thesis_invalid_if (I'll sell if); empty or "
                f"{SOFT_EXIT_UNKNOWN!r} is not a falsifier"
            )
        return self


class TradeDecision(LLMOutputModel):
    model_config = ConfigDict(validate_assignment=True)

    # Stage 3 (shorts): SHORT opens/adds a short (mirror of BUY); COVER
    # reduces/closes one (mirror of SELL). SELL never means "open a short"
    # — every existing consumer already reads SELL as "reduce or close a
    # long", and overloading it would silently reinterpret them.
    action: Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]
    symbol: str
    allocation_pct: float = Field(ge=0, le=100)
    entry_price: float
    stop_loss: float
    take_profit: float
    reasoning: str
    # --- Conviction ledger (QAMC remediation spec §7.2) --------------------
    # Pinned at ENTRY (BUY/SHORT) only, mirroring how `expected_horizon_
    # sessions`/`setup_type` are pinned at entry rather than recomputed
    # later. All three default to None so every pre-existing construction
    # site (HOLD/_build_sell/_build_cover, tests, replay.py, ops/model_
    # policy/scenarios.py) is unaffected.
    #
    # `conviction` mirrors TargetPosition.conviction — the PM's own label
    # for the idea, carried through the constructor unchanged.
    conviction: Literal["high", "medium", "low"] | None = None
    # `requested_risk_pct` is TargetPosition.risk_allocation_pct AS THE PM
    # ASKED FOR IT — before the constructor's single-name clamp or the
    # portfolio/cluster budget rationing touch it. None for a legacy
    # notional (target_weight_pct-only) target, which asked for no risk
    # figure at all.
    requested_risk_pct: float | None = None
    # `allocated_risk_pct` is `RiskPlan.risk_pct` — "what the budget
    # actually granted" per that dataclass's own docstring, i.e. the
    # PRE-clamp request rationed through `allocate_risk_budget` and the
    # single-name ceiling in `_plan_risk_targets`. This is the closest
    # cheaply-available approximation of "what was really used to size the
    # order" — a further downstream notional clamp in `_build_buy`/
    # `_build_short` (the single-name weight ceiling) can
    # still shrink the FINAL position below what this figure implies; that
    # last clamp is not re-derived into a third number here. None when no
    # risk-based plan exists for this symbol (legacy notional target).
    allocated_risk_pct: float | None = None
    # --- Which rule placed the shipping stop (2026-09-02) ----------------
    # One of the `STOP_RULE_*` codes in `src/portfolio_constructor.py`, or
    # None when nothing resolved a stop (SELL/COVER/HOLD, legacy callers,
    # tests). Set by the constructor at the moment the order is built.
    #
    # It exists so the EXECUTION stage can tell a stop that sits at a
    # computed structural level from one the constructor merely accepted.
    # `src/pipeline_stages.py` carries a second, execution-time 1x ATR stop
    # floor, and without this field that floor re-widened level-backed
    # stops the constructor had deliberately honoured under §12.1 — undoing
    # the fix one stage later, against an ATR recomputed from different
    # bars. Recomputing level-backing there instead would have created
    # exactly the second data path §12.1 was careful not to build.
    stop_rule: str | None = None
    # --- The sub-floor catalyst exception, carried to execution (2026-09-11)
    # True when this order was permitted BELOW `min_reward_risk_after_
    # widening` because the PM's sub-floor catalyst gate verified its
    # citation against a real dated state-change row and capped it at
    # starter size. Mirrors `stop_rule` directly above in both purpose and
    # mechanism: a fact the constructor knows and the execution stage cannot
    # re-derive without building a second copy of the gate.
    #
    # `src/pipeline_stages.py` carries a SECOND, execution-time reward:risk
    # belt (a flat 1.2) that only fires when execution changed the geometry
    # the Risk Manager audited. Without this field that belt killed every
    # such order the moment anything drifted — not because execution had
    # degraded the trade, but because the trade was deliberately below 1.2
    # to begin with, which is the condition the exception exists to permit.
    # docs/WORK.md funnel item 4 ("a SECOND reward:risk floor at execution
    # time") says to fold that belt into item 1(b); this is that fold. With
    # the flag set, the belt checks for DEGRADATION instead: execution may
    # not make the ratio worse than the geometry the RM approved.
    subfloor_catalyst_exception: bool = False
    # --- How this position is MANAGED (2026-09-11, WORK.md item 1(d)) -----
    # `TechAnalysisResult.setup_type` for the analysis this order was built
    # from — "range" (Type A) or "breakout" (Type B) — or None for
    # SELL/COVER/HOLD, a legacy caller, or a target with no analysis.
    #
    # Carried for exactly the same reason as `stop_rule` and
    # `subfloor_catalyst_exception` above: a fact the constructor knows and
    # the execution stage cannot re-derive without building a second copy of
    # it. Two consumers:
    #   - `src/pipeline_stages.py`'s execution-time reward:risk belt, which
    #     must not run at all for a breakout (no overhead level exists to
    #     measure a reward against — see
    #     `src.risk.constants.reward_risk_floor_applies`);
    #   - `src/agents/risk_manager.py`'s rendering of the order, which must
    #     not show a Risk Manager an "R/R x:1" figure for a trade whose
    #     approval never depended on one.
    setup_type: str | None = None
    # --- Thesis invalidation, as a real field (2026-09-03) ----------------
    # Mirrors the conviction-ledger fields above: pinned at ENTRY (BUY/
    # SHORT) only, default None so every pre-existing construction site
    # (HOLD/_build_sell/_build_cover, tests, replay.py, ops/model_policy/
    # scenarios.py) is unaffected.
    #
    # `TargetPosition.thesis_invalid_if` (the analyst's own falsifier
    # condition, see that field's docstring) was previously carried
    # downstream ONLY as text appended to `reasoning` — e.g. "(invalid if:
    # ...)" — and `reasoning` gets truncated to 500 chars in the
    # constructor and again to 280 chars by `TradingPipeline.
    # _build_position_history`. A long condition could silently lose the
    # one piece of information a later holding-discipline check needs to
    # verify an exit. This field carries the same string untruncated
    # (aside from the 2000-char cap below, which exists only to bound a
    # misbehaving LLM's output, not to truncate a normal thesis) so it
    # survives regardless of what happens to `reasoning`.
    #
    # The embedded-in-reasoning text is NOT removed — some existing code
    # may still parse it out of `reasoning` — this field is additive.
    thesis_invalid_if: str | None = Field(default=None, max_length=2000)

    @computed_field
    @property
    def reward_risk(self) -> float | None:
        """Reward/risk ratio of the CONSTRUCTED order, computed in Python.

        Deliberately mirrors `TechAnalysisResult.risk_reward` — including its
        "not trusted to the LLM" rule — because the object that ACTUALLY
        REACHES EXECUTION never got that treatment, and the omission cost a
        live trading session on 2026-08-31.

        What happened: the Risk Manager is handed entry/stop/target as bare
        text with no ratio, so it does the division inside the model. For a
        BUY on RSG (entry $221.14, stop $207.90, target $242.96) it computed
        the ratio TWICE IN ONE RESPONSE — `rr_audit` said "R/R = 1.65 ...
        above 1.5, so compliant", while `reasoning` said "R/R = 1.31, which
        is below the 1.5 floor" and rejected the trade. The pipeline acts on
        the second field. 1.65 is correct; 1.31 matches no combination of the
        inputs and was simply wrong.

        An LLM must not own the arithmetic of a gate. It judges; the
        deterministic side computes. Geometry rules match the tech-analyst
        field: prices must be present and the inequalities must hold for the
        side, else None, so nobody renders a fake ratio.

        Since 2026-09-02 the division itself lives in `reward_to_risk`, so
        this field, `TechAnalysisResult.risk_reward`, the constructor's
        entry gate and the execution-time re-check are all the SAME
        arithmetic on whatever geometry each is handed. See that function
        for the XLE 1.67-vs-1.18 rejection that forced it.
        """
        if self.action == "BUY":
            is_short = False
        elif self.action == "SHORT":
            is_short = True
        else:
            # SELL / COVER reduce an existing position; HOLD opens nothing.
            # No entry geometry to measure, so no ratio exists.
            return None
        ratio = reward_to_risk(
            self.entry_price, self.stop_loss, self.take_profit,
            is_short=is_short,
        )
        return None if ratio is None else round(ratio, 2)
    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # action is UPPERCASE per Literal — fold LLM drift like "buy".
        return _normalize_enum_case_fields(values, upper_fields=("action",))

    @model_validator(mode="after")
    def validate_buy_prices(self):
        if self.action == "BUY":
            if self.entry_price <= 0:
                raise ValueError("BUY decisions require entry_price > 0")
            if self.stop_loss < 0:
                raise ValueError("BUY decisions require stop_loss >= 0")
            if self.take_profit <= 0:
                raise ValueError("BUY decisions require take_profit > 0")
            if self.stop_loss > 0 and self.stop_loss >= self.entry_price:
                raise ValueError(
                    "BUY decisions require stop_loss to stay below entry_price"
                )
            if self.take_profit <= self.entry_price:
                raise ValueError(
                    "BUY decisions require take_profit to stay above entry_price"
                )
        elif self.action == "SHORT":
            # Mirror of the BUY geometry: a short's stop protects ABOVE
            # entry and its take-profit sits BELOW entry (price must fall
            # for a short to profit).
            if self.entry_price <= 0:
                raise ValueError("SHORT decisions require entry_price > 0")
            if self.stop_loss < 0:
                raise ValueError("SHORT decisions require stop_loss >= 0")
            if self.take_profit <= 0:
                raise ValueError("SHORT decisions require take_profit > 0")
            if self.stop_loss > 0 and self.stop_loss <= self.entry_price:
                raise ValueError(
                    "SHORT decisions require stop_loss to stay above entry_price"
                )
            if self.take_profit >= self.entry_price:
                raise ValueError(
                    "SHORT decisions require take_profit to stay below entry_price"
                )
        # SELL and COVER don't need live entry/stop/target — execution uses
        # market price, exactly as SELL always has.
        return self


class ReasoningChain(LLMOutputModel):
    """7-step CoT for the portfolio manager — forces the audit trail on the
    central decision. Every required field has `min_length=1` so the LLM
    can't dodge a step with `""`. continuity_check AND premortem_check are
    intentionally optional (default `""`) for backward-compat with older logs
    (pre-memory-layer / pre-2026-06 respectively) but are mandatory per the
    prompt; everything else is mandatory at the schema layer too.
    """
    macro_filter: str = Field(min_length=1)
    news_check: str = Field(min_length=1)
    earnings_check: str = Field(min_length=1)
    signal_conflicts: str = Field(min_length=1)
    sizing_logic: str = Field(min_length=1)
    portfolio_balance: str = Field(min_length=1)
    cash_target: str = Field(min_length=1)
    # Continuity check — narrates how today's decisions fit the 7-day arc.
    # Optional (old logs don't carry it) but required when memory layers are provided.
    continuity_check: str = ""
    # Pre-mortem — the disconfirming/red-team step. The strongest case AGAINST
    # today's biggest position(s) + the single observable that would prove the
    # thesis wrong. Optional-default for backward-compat with pre-2026-06 logs
    # (same pattern as continuity_check) but MANDATORY per the prompt — its job
    # is to catch the systematic directional bias a forward-only CoT misses.
    premortem_check: str = ""
    # Macro logic audit — the answer to a question the prompt had been asking
    # with nowhere to put the reply. PM's rendered briefing ships the macro
    # seat's full six-paragraph `reasoning_chain` verbatim under the heading
    # "audit these for logic errors", and until 2026-09-14 this schema had no
    # field of any kind that such a finding could land in. Across the archived
    # PM calls that carried those paragraphs, no response ever named a macro
    # logic error — which is what an instruction with no output channel looks
    # like from the outside. That is a structural argument, not a measured
    # improvement: this desk has no rig that can validate a prompt rewrite
    # (the rehearsal rig replays recorded answers into a changed prompt and
    # passes regardless), so the claim rests on "no field existed", which is
    # checkable by reading this class, and not on a benchmark.
    #
    # Optional-default at the schema layer for the same reason as the two
    # fields above — every archived log predates it — but MANDATORY per the
    # prompt. Deliberately NOT `min_length=1`: forcing a non-empty string
    # when the chain is sound is how the fabricated `or "n/a"` placeholders
    # started (see `ExitReviewChain`). "No logic error found" is a real
    # verdict and the prompt asks for it; an invented one is not.
    macro_audit: str = ""


class ExitReviewChain(ReasoningChain):
    """The POSITION REVIEWER's chain, in the container the risk seat reads.

    `_risk_review_exits` carries the reviewer's own six checks in a
    `ReasoningChain` because that is the container `RiskManagerAgent` renders;
    `risk_review_mode._CHAIN_ROWS[EXIT_REVIEW]` relabels each slot with the
    reviewer's real field name. One PM slot has no counterpart at all —
    `news_check` — and is not rendered to the seat on this path. It was
    nonetheless being filled with a placeholder string ("[unused on the
    exit-review path...]") for one reason only: `ReasoningChain` makes it
    `min_length=1`. Inventing content to satisfy a constraint that does not
    apply is how the fabricated `or "n/a"` placeholders started.

    So this subclass relaxes exactly that one field, and only for the exit
    path. It is a deliberate narrowing of a parent constraint, kept to a
    single field so the relaxation is legible: the MORNING path never
    constructs this class, and `ReasoningChain.news_check` stays mandatory
    there. `continuity_check` and `premortem_check` are already optional on
    the parent and are never rendered on this path.
    """
    news_check: str = ""


class AnalystProvenance(LLMOutputModel):
    """Machine-checkable specialist claim supporting a PM target.

    ``relationship=conflicts`` is an explicit, legitimate PM disagreement;
    it is not a veto.  The PM boundary verifies that the named specialist
    actually covered the symbol and that ``observed_stance`` matches its
    validated output.
    """

    source: Literal["technical", "news", "earnings", "macro", "smart_money"]
    observed_stance: str = Field(min_length=1)
    relationship: Literal["supports", "conflicts", "context"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _normalize_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("source", "observed_stance", "relationship"),
        )


class InsiderPurchaseCluster(BaseModel):
    """Two or more distinct insiders buying the same stock on the same day.

    Computed in Python by
    `src.data.smart_money_cluster.insider_purchase_clusters` from SEC Form 4
    rows only; never parsed from a model response. The definition is the one
    Alldredge & Blank measure (J. Financial Research 42(2), 2019; SSRN
    2781761): a purchase "on the same day as another insider purchase at the
    same company". Members are restricted to open-market purchases (Form 4
    code P) that the routine test in `src.data.insider_signal` classified
    OPPORTUNISTIC, because Cohen, Malloy & Pomorski (NBER w16454) find routine
    trades earn "essentially zero" abnormal return.

    `filing_age_days` is today minus the latest member filing's disclosure
    date — how long ago the cluster became fully knowable. It changes daily
    and is therefore excluded from the synthesis evidence hash.
    """
    transaction_date: date
    distinct_insiders: int = Field(ge=2)
    insider_ciks: list[str] = Field(min_length=2)
    combined_value_usd: float = Field(ge=0)
    latest_disclosure_date: date
    filing_age_days: int = Field(ge=0)


class SmartMoneyObservation(LLMOutputModel):
    """Source-backed smart-money fact; timestamps and amounts are source facts.

    Congressional fields remain optional-compatible with records already stored
    by the first provider.  SEC Form 4 observations add accession-level
    provenance and never rely on the LLM to classify a transaction code.
    """
    symbol: str
    stream: Literal["congressional", "insider"] = "congressional"
    actor: str = Field(min_length=1)
    actor_cik: str = ""
    actor_roles: list[str] = []
    joint_owner_ciks: list[str] = []
    direction: Literal["buy", "sell", "exchange", "unknown"]
    amount_range: str = ""
    transaction_date: date
    disclosure_date: date
    accepted_at: datetime | None = None
    known_at: datetime | None = None
    source_url: str = Field(min_length=1)
    accession_number: str = ""
    filing_form: Literal["", "4", "4/A"] = ""
    transaction_code: Literal["", "P", "S"] = ""
    transaction_row: int | None = Field(default=None, ge=0)
    security_title: str = ""
    shares: float | None = Field(default=None, ge=0)
    price_per_share: float | None = Field(default=None, ge=0)
    transaction_value_usd: float | None = Field(default=None, ge=0)
    post_transaction_shares: float | None = Field(default=None, ge=0)
    ownership_nature: Literal["", "direct", "indirect", "unknown"] = ""
    amendment: bool = False
    late_filing: bool = False
    is_10b5_1: bool | None = None
    listed_exchange: str = ""
    in_core_universe: bool = False
    in_trading_universe: bool = False
    admission_eligible: bool = False
    transient_admission_eligible: bool = False
    transient_admitted: bool = False
    lag_days: int = Field(ge=0)
    disclosure_age_days: int = Field(ge=0)
    freshness: Literal["fresh", "delayed", "stale"]
    # Routine/opportunistic verdict from src.data.insider_signal. Defaults are
    # empty so rows cached before the classifier existed still validate; they
    # are populated deterministically on every ``fetch``.
    signal_class: Literal["", "opportunistic", "routine", "indeterminate"] = ""
    signal_class_reason: str = ""
    signal_class_detail: str = ""
    signal_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    # Trade size relative to what the insider already held, reconstructed from
    # the Form 4 fields the desk already parses (`shares` and
    # `post_transaction_shares`) — see
    # `src/data/insider_signal.py::holdings_fraction`. Reported for buys and
    # sells alike and never used as an admission cutoff; the bands are Scott &
    # Xu's own (FAJ 2004). `None`/`""` when the filing does not carry enough
    # to compute one, and `no_prior_holding` for a purchase by an insider who
    # held nothing beforehand (no ratio exists — that is a distinct fact, not
    # a missing one).
    holdings_fraction: float | None = Field(default=None, ge=0.0)
    holdings_fraction_band: Literal[
        "", "under_10pct", "10_to_50pct", "over_50pct", "no_prior_holding"
    ] = ""
    economic_role: Literal["actionable", "confirmatory", "contradictory", "historical"]
    # Populated only for stream="congressional" (src/data/congressional_trading.py).
    # Two independent free sources (kadoa-org/congress-trading-monitor,
    # congresswatch.us) are merged there; when both carry a record for what
    # looks like the same real trade, "agreement" means they matched on
    # direction and overlapping amount bracket, "discrepancy" means they did
    # not (the disagreement is never silently resolved — see
    # `cross_source_note`), and "single_source" means only one source had
    # the trade. Defaults to "" for every SEC Form 4 row and for any
    # congressional row cached before this field existed.
    cross_source_agreement: Literal["", "single_source", "agreement", "discrepancy"] = ""
    cross_source_note: str = ""
    # The most recent same-day opportunistic insider purchase cluster in this
    # row's symbol, stamped by `SECForm4Provider.fetch` on every insider row
    # of a CONFIGURED-UNIVERSE symbol (never on a non-universe or
    # congressional row). A per-symbol fact carried on each row so it
    # survives whichever rows the materiality filter and the observation cap
    # keep. Its only effect is `SmartMoneyFinding.to_verdict`'s
    # medium -> high conviction lift; it never changes sorting or admission.
    purchase_cluster: InsiderPurchaseCluster | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def normalize_form4_aliases(self):
        if self.stream == "insider":
            if self.known_at is None:
                self.known_at = self.accepted_at
            if self.accepted_at is None:
                self.accepted_at = self.known_at
            if self.admission_eligible or self.transient_admission_eligible:
                eligible = (
                    self.direction == "buy" and self.transaction_code == "P"
                    # A routine purchase carries no predictive power, so it can
                    # never be the reason a symbol is admitted to the trading
                    # surface. This only ever narrows admission.
                    and self.signal_class != "routine"
                )
                self.admission_eligible = eligible
                self.transient_admission_eligible = eligible
        return self


#: `SmartMoneyFinding.economic_role` -> `AnalystVerdict.conviction`. NEW
#: JUDGMENT, not a restatement (the finding carries no confidence field to
#: read off). The ordering is not invented here: `_ROLE_RANK` in
#: `src/agents/smart_money_analyst.py` already ranks these four labels
#: actionable(3) > confirmatory(2) > contradictory(1) > historical(0) to
#: decide which fact the seat surfaces first, and the seat's own prompt
#: explains WHY — "actionable" is present-tense, source-backed trading
#: evidence; "confirmatory" is thematic context (congressional/13F, filed
#: up to 45 days late); "historical" is stale and cannot support a target
#: (`build_evidence_registry`/`stance_is_aligned` refuse it); "contradictory"
#: describes a fact's RELATIONSHIP to the current thesis, not its own
#: strength, so folding it in at the bottom alongside "historical" is the
#: more conservative of two readings, not the only defensible one. Squeezed
#: onto the desk's 3-rung high/medium/low scale, contradictory and
#: historical collapse to the same "low" rung. Flag for review.
_SMART_MONEY_ROLE_CONVICTION: dict[str, str] = {
    "actionable": "high",
    "confirmatory": "medium",
    "contradictory": "low",
    "historical": "low",
}

#: DELETED 2026-09-13 (retired item 31): `_SMART_MONEY_ROLE_MAGNITUDE`,
#: an unsourced {actionable 1.0, confirmatory 0.6, contradictory 0.3,
#: historical 0.3} table keyed on the SAME `economic_role` that
#: `_SMART_MONEY_ROLE_CONVICTION` above is keyed on. Both halves of
#: `score_verdict` therefore read one categorical label, so the composite
#: counted it twice and an "actionable" finding alone scored the maximum.
#: Magnitude is now `NO_STATED_STRENGTH` (0.0 — this seat has no strength
#: scale of its own, and does not borrow one); the role still sets
#: conviction, which is the one place it has a derivation behind it.


def _purchase_cluster_lift(
    conviction: str,
    direction: str,
    cluster: "InsiderPurchaseCluster | None",
) -> str:
    """Owner ask 2026-09-19 (board item 124): a confirmed same-day
    opportunistic insider purchase cluster lifts this seat's conviction from
    "medium" to "high" on a bullish read, and does nothing else.

    Why only buying and only one rung: Alldredge & Blank (2019) report
    abnormal returns after CLUSTERED PURCHASES; nothing in that source is
    about sales, and a purchase cluster says nothing in favour of a bearish
    call. The lift is capped at the existing high rung — this seat's
    conviction is a three-rung label, and a cluster cannot create a rung the
    table does not have, nor rescue a "low" (contradictory/historical) read,
    which the table puts there for reasons the cluster does not address.
    """
    if cluster is not None and direction == "bullish" and conviction == "medium":
        return "high"
    return conviction


class SmartMoneyFinding(LLMOutputModel):
    symbol: str
    stance: Literal["bullish", "bearish", "neutral", "mixed"]
    economic_role: Literal["actionable", "confirmatory", "contradictory", "historical"]
    summary: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    # All four are `SkipJsonSchema`: the desk fills every one of them itself,
    # and before this they were REQUIRED output under strict structured
    # output. `SmartMoneyAnalystAgent._parse_findings` overwrites
    # `observations` with `[o.model_dump() for o in source_rows]` and
    # `evidence_hash` with the desk's own digest on every single finding,
    # cached or live, and `deterministic_eligibility` below recomputes both
    # eligibility booleans from the source rows. So the seat was spending its
    # output budget re-typing a 45-field internal row (accession_number,
    # transaction_row, signal_weight, freshness, admission_eligible,
    # transient_admitted, in_core_universe, lag_days, ...) at least once per
    # finding, plus a sha256 it cannot know, and 100% of it was discarded
    # before the object was constructed. Nothing validates the echo against
    # the source rows, so it was never a grounding device either.
    observations: Annotated[list[SmartMoneyObservation], SkipJsonSchema()] = Field(min_length=1)
    support_eligible: Annotated[bool, SkipJsonSchema()] = False
    transient_admission_eligible: Annotated[bool, SkipJsonSchema()] = False
    evidence_hash: Annotated[str, SkipJsonSchema()] = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def deterministic_eligibility(self):
        streams = {o.stream for o in self.observations}
        directional = {
            o.direction for o in self.observations
            if o.direction in {"buy", "sell"}
        }
        # 2026-09-11, owner redesign: a calendar-age cutoff on WHETHER this
        # evidence can support a thesis is gone. Owner's framing, direct:
        # "this is one piece of information — if it doesn't correlate with
        # anything else, that's fine, it just changes the decision matrix;
        # if it does correlate, stronger weights." An insider trade is a
        # fact that happened; it does not expire on a clock. Real research
        # backs treating it this way rather than a short fixed window:
        # Seyhun (1986) found only ~1/4 of the eventual abnormal return from
        # an insider purchase realizes in the first 5 days and ~1/2 is still
        # unrealized after a full month; pre-announcement run-ups in real
        # M&A data are documented starting MONTHS before the news breaks.
        # A 7-day cutoff was never grounded in either fact.
        #
        # What decides whether this can actually support a target now is
        # CORRELATION with other CURRENT evidence, checked where that
        # evidence is actually visible together —
        # `PortfolioManagerAgent`'s grounding validator, which requires at
        # least one other live source (technical/news/earnings/macro) to
        # currently agree with this finding's direction before smart_money
        # may be marked `supports` rather than `context`. This model only
        # still enforces STRUCTURAL validity — is this real, single-
        # direction, legally-disclosed evidence at all — never how old it
        # is.
        if streams == {"congressional"}:
            # Preserve the original conservative congressional contract.
            # lag_days cap raised 30 -> 45: the STOCK Act's own legal filing
            # deadline is 45 days after the transaction, so a lag of up to
            # 45 days is a legally on-time disclosure, not a stale one. This
            # is about LEGAL disclosure timing (when we were allowed to
            # learn about the trade), not the trade's own informational
            # age — a different question, kept.
            actors = {o.actor.strip().casefold() for o in self.observations}
            self.support_eligible = (
                len(self.observations) >= 2
                and len(actors) >= 2
                and len(directional) == 1
                and all(o.lag_days <= 45 for o in self.observations)
            )
            # Owner ruling 2026-09-19 (docs/INCIDENT_HISTORY.md, 2026-09-20
            # entry): congressional disclosures are evidence and must never
            # be zeroed out, but their ceiling is "confirmatory" — they may
            # raise a thesis's conviction, never alone reach "actionable"
            # present-tense trading evidence. A same-day cluster of several
            # members (the >=2-actor gate above) therefore lifts conviction
            # by AT MOST ONE step, historical/low -> confirmatory/medium; it
            # is capped here, not scaled by how many members clustered, so a
            # 2-member and a 10-member cluster land on the same rung.
            if not self.support_eligible:
                self.economic_role = "historical"
            elif self.economic_role == "actionable":
                self.economic_role = "confirmatory"
            self.transient_admission_eligible = False
            return self

        # SEC observations have already passed the provider's deterministic
        # materiality/cluster filter. Real, one-direction evidence may
        # support PM provenance (subject to the correlation check above).
        # Only an explicit open-market purchase can enter the separately
        # governed transient-candidate lane, which keeps its own,
        # deliberately stricter freshness bar (`admission_eligible` in
        # `src/data/smart_money.py`) — a single stale signal must never
        # alone justify pulling a brand-new symbol into the universe, which
        # is a different risk than confirming a thesis on a symbol already
        # in play.
        self.support_eligible = (
            bool(directional)
            and len(directional) == 1
        )
        self.transient_admission_eligible = any(
            o.transient_admission_eligible
            and o.transaction_code == "P"
            and o.direction == "buy"
            for o in self.observations
        )
        # Same honesty rule as the congressional branch above: the model's
        # own economic_role self-report must not survive a deterministic
        # "too thin" verdict. Without this, an SEC/insider finding whose
        # only observation is stale (or whose directions don't agree) could
        # still claim "actionable" and reach the PM at full magnitude/high
        # conviction via to_verdict() while support_eligible is False.
        if not self.support_eligible:
            self.economic_role = "historical"
        return self

    def purchase_cluster(self) -> "InsiderPurchaseCluster | None":
        """The deterministic purchase cluster stamped on this finding's
        insider rows, or None. Congressional rows never carry one."""
        stamped = [
            o.purchase_cluster for o in self.observations
            if o.stream == "insider" and o.purchase_cluster is not None
        ]
        if not stamped:
            return None
        return max(stamped, key=lambda c: c.transaction_date)

    def to_verdict(self) -> "AnalystVerdict":
        """This finding, restated in the shared Phase 13 verdict shape.

        UNLIKE `TechAnalysisResult.to_verdict`, this is only a PARTIAL
        restatement — see `_SMART_MONEY_ROLE_CONVICTION` above for the one
        field that is new judgment rather than a value already sitting on
        this model. (There used to be a second, `_SMART_MONEY_ROLE_MAGNITUDE`;
        it was deleted on review — see the note where it stood.)

        direction    — `stance`, with "mixed" folded into "neutral". This
                       is a restatement of existing desk convention, not a
                       new call: `PortfolioManagerAgent._collapse_stances`
                       and `_stance_matches_source`
                       (src/agents/portfolio_manager.py,
                       src/agents/smart_money_analyst.py) already treat
                       "mixed" and "neutral" as the same non-directional
                       bucket — conflicting buy/sell activity supports
                       neither a bullish nor a bearish call.
        magnitude    — `NO_STATED_STRENGTH` (0.0), directional or not. This
                       seat states no strength independent of
                       `economic_role`, and `economic_role` already drives
                       conviction, so a magnitude derived from it would be
                       the same signal counted twice — and a magnitude
                       borrowed off Technical's rungs would be a number this
                       seat has no scale for. The seat still reaches the
                       ranking through its weighted conviction; see
                       `NO_STATED_STRENGTH` and `src/verdicts.py`.
        conviction   — `_SMART_MONEY_ROLE_CONVICTION[economic_role]`. New
                       judgment. Then ONE deterministic lift, owner ask
                       2026-09-19 (board item 124): a bullish finding whose
                       insider rows carry a confirmed same-day opportunistic
                       purchase cluster (`InsiderPurchaseCluster`) is lifted
                       from "medium" to "high". Nothing else moves: "low"
                       stays low, "high" cannot go higher, a bearish or
                       neutral read is untouched, and the model has no say —
                       it may mention the cluster, the code sets the rung.
                       See `_purchase_cluster_lift`.
        evidence     — `summary` and `why_now`, each as one labelled item
                       when present, plus up to 5 observations (most recent
                       transaction_date first) summarized as text.
        invalidation — SmartMoneyFinding has no invalidation-style field to
                       restate. Left "" for a neutral verdict (allowed).
                       For a directional verdict the base model REQUIRES a
                       non-empty invalidation, so one is constructed from
                       `why_now`, framed as a condition: the call stands
                       only while that stated reasoning holds. This is
                       genuinely invented, not read off the model — flagged
                       for review, not presented as a restatement.
        """
        stance = "neutral" if self.stance in ("neutral", "mixed") else self.stance
        # 0.0 either way — a neutral read has no lean, and a directional
        # read from this seat states no distance. See `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH
        conviction = _SMART_MONEY_ROLE_CONVICTION[self.economic_role]
        cluster = self.purchase_cluster()
        conviction = _purchase_cluster_lift(conviction, stance, cluster)

        evidence: list[VerdictEvidence] = []
        if self.summary.strip():
            evidence.append(VerdictEvidence(label="summary", text=self.summary.strip()))
        if self.why_now.strip():
            evidence.append(VerdictEvidence(label="why_now", text=self.why_now.strip()))
        if cluster is not None:
            evidence.append(VerdictEvidence(
                label="insider_purchase_cluster",
                value=cluster.combined_value_usd,
                as_of=cluster.transaction_date,
                text=(
                    f"{cluster.distinct_insiders} distinct insiders made "
                    "opportunistic open-market purchases on "
                    f"{cluster.transaction_date.isoformat()}, combined "
                    f"${cluster.combined_value_usd:,.0f}; latest filing "
                    f"{cluster.filing_age_days} days old"
                ),
            ))
        most_recent = sorted(
            self.observations, key=lambda o: o.transaction_date, reverse=True,
        )[:5]
        for obs in most_recent:
            detail = f"{obs.actor}: {obs.direction}"
            if obs.amount_range:
                detail += f" {obs.amount_range}"
            detail += f" on {obs.transaction_date.isoformat()}"
            evidence.append(VerdictEvidence(label="observation", text=detail))

        invalidation = ""
        if stance != "neutral":
            invalidation = (
                f"the why-now premise no longer holds: {self.why_now.strip()}"
            )

        return AnalystVerdict(
            seat="smart_money",
            symbol=self.symbol,
            direction=stance,
            magnitude=magnitude,
            conviction=conviction,
            evidence=evidence,
            invalidation=invalidation,
        )


class SmartMoneySynthesis(LLMOutputModel):
    """Top-level shape of the smart money analyst's response: a single
    `findings` list. Exists ONLY to declare the OpenRouter response_format
    schema (src/agents/base.py) — the agent still reads `parsed["findings"]`
    and validates each entry as SmartMoneyFinding directly, per-entry
    isolated (src/agents/smart_money_analyst.py._parse_findings); this model
    is never itself constructed from a response."""

    findings: list[SmartMoneyFinding]


class TargetPosition(LLMOutputModel):
    """PM's per-symbol intent — WHAT the book should look like, not HOW to get there.

    The PortfolioConstructor translates a list of TargetPositions + current
    holdings + market prices + TA ATR into concrete TradeDecision orders. The
    LLM no longer guesses entry prices, stops, or share counts — it only
    expresses intent.

    Semantics:
    - target_weight_pct = 0 and symbol currently held → close the position.
    - target_weight_pct > 0 on a new symbol → open.
    - target_weight_pct > current weight → add (partial BUY for the delta).
    - target_weight_pct < current weight → trim (partial SELL for the delta).
    - Held symbols NOT appearing in the target list → hold at current weight
      (no instruction = no change). PM may include them explicitly with a
      `keep` note for audit clarity, but it's not required.
    """

    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    # Stage 3 (shorts). Default "long" so every stored decision — every
    # historical agent_logs / specialist_evidence row parsed through this
    # model, and every live target a PM prompt that doesn't yet know this
    # field exists ever emits — replays with EXACTLY the behaviour it had
    # before this field existed. The constructor turns this + the
    # (unsigned) size field into a SIGNED target weight:
    #     signed_target = -target_pct if direction == "short" else +target_pct
    # `current_pct` (see `_current_weights`) is already signed — negative
    # means a held short. Delta math then operates on signed weights: a
    # positive delta is buy-side (BUY to open/add a long, or COVER to
    # reduce a short); a negative delta is sell-side (SELL to reduce a
    # long, or SHORT to open/add a short).
    direction: Literal["long", "short"] = "long"
    # --- Sizing (Phase 2b, 2026-08-27) -------------------------------------
    # Conviction is expressed as RISK, not as notional weight.
    #
    # `target_weight_pct` is risk-blind: a 3% position stopped 10% away risks
    # 0.3% of equity; the same 3% stopped 2% away risks 0.06%. The PM was
    # choosing the number that does NOT determine what a losing trade costs,
    # while the number that does — the distance to the stop — was set by
    # somebody else entirely.
    #
    # `risk_allocation_pct` is the share of equity this idea may lose if its
    # stop is hit. The constructor derives share count from it:
    #     shares = (equity x risk_pct / 100) / |entry - stop|
    # A wider stop therefore yields a SMALLER position rather than a rejected
    # trade, which eliminates the entire "stops too tight" failure class:
    # risk is never controlled by squeezing the stop.
    #
    # Envelope is owner-ratified (2026-08-27): 5% ceiling, 0.5% floor, below
    # which the idea is not worth trading. 0.0 is legal and means CLOSE.
    risk_allocation_pct: float | None = Field(default=None, ge=0.0, le=5.0)
    # Legacy notional sizing. Retained ONLY so historical agent_logs and
    # specialist_evidence rows still parse — `src/replay.py` and the Mission
    # Control API both re-validate stored PM output through this model. New
    # live decisions must supply `risk_allocation_pct`; the grounding
    # validator enforces that. When both are present, risk wins.
    target_weight_pct: float | None = Field(default=None, ge=0.0, le=20.0)
    conviction: Literal["high", "medium", "low"] = "medium"
    thesis: str
    # Same null-coercion as TechAnalysisResult above, same measured reason: a
    # model emitting an explicit null here must not invalidate the target.
    # (No production null observed on this field — 272 occurrences, 0 nulls —
    # but the tech-side field is the same prompt instruction to the same
    # models, so the exposure is real even though it has not fired yet.)
    thesis_invalid_if: str = ""

    @field_validator("thesis_invalid_if", "catalyst", mode="before")
    @classmethod
    def _null_soft_exit_is_unknown_not_a_wipe(cls, v, info: ValidationInfo):
        """A target is always an actionable intent, so an explicit null
        records `unknown` rather than wiping a stated soft-exit to silent
        empty. Never invents a falsifier or catalyst string."""
        if isinstance(v, str) and v.strip():
            return v
        if v is None:
            return SOFT_EXIT_UNKNOWN
        return v

    # Optional override hints the constructor MAY use. Non-binding — if
    # absent, the constructor falls back to TA's ATR-based stop (2*ATR) and
    # the broker's live price for entry.
    suggested_stop_price: float | None = None
    catalyst: str = ""  # populated when target violates R/R < 1.5 discipline
    # --- The sub-floor catalyst exception, as a CHECKED fact (2026-09-11) --
    # Set to True by `PortfolioManagerAgent._apply_subfloor_catalyst_rule`,
    # and by nothing else, for a below-floor target whose `catalyst` cited
    # the ISO date of a real `active_state_changes` row that names this
    # symbol in the direction this trade needs — and which that same gate
    # then capped at `STARTER_POSITION_RISK_PCT`.
    #
    # It exists because the exception was previously INERT end to end. The
    # PM gate verified the citation and capped the size, and then
    # `PortfolioConstructor._widen_stop_past_noise` refused the order
    # anyway on `min_reward_risk_after_widening` — the same floor, applied
    # a second time one stage later, with no knowledge that the exception
    # had been granted. Measured 2026-09-11 on a level-backed stop with a
    # real structural target at reward:risk 1.2: the PM kept and capped it,
    # the constructor returned `(None, None)`. So a verified catalyst could
    # never produce a trade, which made parts (b) and (c) of docs/WORK.md
    # item 1 decorative.
    #
    # NOT MODEL-SETTABLE, and that is the whole point of making it a PRIVATE
    # attribute rather than a field: prompt compliance is not a control, so
    # the flag the constructor trusts must be one only Python can write. A
    # private attribute cannot be supplied in input at all — not by raw LLM
    # JSON, and not by a stored historical row re-validated through this
    # model by `src/replay.py` or the Mission Control API, which would
    # otherwise be claiming a check that never ran. Stripping a public field
    # in a validator was tried and is NOT equivalent: pydantic runs
    # model-level before-validators on the `validate_assignment` path too,
    # so the same strip that blocks the model also blocks the gate.
    #
    # Read it via `subfloor_catalyst_verified`; write it only via
    # `mark_subfloor_catalyst_verified()`. The PM decision object is handed
    # to `PortfolioConstructor.construct_orders` directly (see
    # `src/pipeline_stages.py`) with no serialization hop in between, so a
    # private attribute survives the whole journey it needs to.
    _subfloor_catalyst_verified: bool = PrivateAttr(default=False)

    @property
    def subfloor_catalyst_verified(self) -> bool:
        """Was a sub-floor reward:risk catalyst exception GRANTED for this
        target by `PortfolioManagerAgent._apply_subfloor_catalyst_rule`?

        Read-only on purpose. See `_subfloor_catalyst_verified` above.
        """
        return self._subfloor_catalyst_verified

    def mark_subfloor_catalyst_verified(self) -> None:
        """Record that the exception was granted. Called by the PM gate, in
        the same breath as the starter-size cap, and by nothing else."""
        self._subfloor_catalyst_verified = True
    # Default preserves read compatibility for historical agent logs.  New
    # live PM decisions are required to populate this by the deterministic
    # PM grounding validator before they may reach PortfolioConstructor.
    provenance: list[AnalystProvenance] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_enum_case_fields(values, lower_fields=("conviction",))
        # Common plain-English synonym emitted by otherwise valid PM plans.
        # This is a confidence label only; mapping moderate→medium changes no
        # target, holding, risk, or execution authority.
        if isinstance(values, dict) and values.get("conviction") == "moderate":
            values["conviction"] = "medium"
        return values

    @model_validator(mode="after")
    def _requires_one_sizing_field(self):
        """A target must size itself somehow.

        Both fields are Optional so historical rows (which carry only
        `target_weight_pct`) still parse, but a target carrying NEITHER is
        meaningless — it names a symbol and asks for nothing. Rejecting it
        here keeps the "drop malformed entries" path in the PM agent from
        having to special-case a silently zero-sized position.
        """
        if self.risk_allocation_pct is None and self.target_weight_pct is None:
            raise ValueError(
                f"{self.symbol}: target supplies neither risk_allocation_pct "
                f"nor target_weight_pct — it sizes to nothing"
            )
        return self

    @property
    def is_close(self) -> bool:
        """PM asking to exit this name entirely."""
        if self.risk_allocation_pct is not None:
            return self.risk_allocation_pct == 0.0
        return self.target_weight_pct == 0.0

    @property
    def missing_open_falsifier(self) -> bool:
        """Open/increase intent with no real thesis_invalid_if.

        Target-only view: a full close is exempt; a non-zero trim is
        not classified here because that needs the live book. The
        ticket-book gate classifies via `_target_intent` and passes
        `intent` so reductions are admitted. Enforced at the ticket
        book (heal + one paid retry, then refuse), not as a
        ValidationError — stored historical rows with an empty field
        must still parse. Catalyst stays optional here.
        """
        return open_target_missing_falsifier(self)


#: The named grounds on which the portfolio manager may drop a candidate it
#: was shown. This is a VOCABULARY, not a gate: nothing here is consulted
#: before a trade, no threshold reads it, and adding or removing a code
#: cannot change what the desk is allowed to buy. What it changes is that
#: "why was this name dropped" has an answer that two different sessions can
#: be COMPARED on.
#:
#: Why an enum and not free text (2026-09-18, board item 133). The jam
#: detector (`src/refusal_signature.py`) separates a jammed gate from a quiet
#: market by asking whether every candidate died for the SAME reason while
#: the candidates changed. Free prose defeats that from both ends: it varies
#: with wording where the cause is identical (a missed alarm), and it cannot
#: be counted. A code beside the prose is the shape the constructor's own
#: refusals already use (`refusal=` / `fault=` on the deterministic-gate
#: events), so this is the established pattern rather than a new one.
#:
#: The codes name causes the seat can actually have, and deliberately do NOT
#: mirror any Python gate — a PM rejection is the seat's own judgement, and
#: the deterministic gates record their own refusals separately.
CANDIDATE_REJECTION_CODES: tuple[str, ...] = (
    # the evidence itself
    "evidence_insufficient",      # too few current sources to justify risk
    "evidence_conflicts",         # sources disagree materially, unresolved
    "evidence_stale",             # what exists is too old to act on
    # the idea
    "thesis_not_compelling",      # evidence present; the setup does not earn a slot
    "no_readable_structure",      # no level to enter or invalidate against
    "reward_not_worth_risk",      # the seat's own read of the payoff
    "event_risk",                 # earnings/known event too close
    # the book
    "risk_budget_full",           # no portfolio risk budget left for a new name
    "no_deployment_headroom",     # no cash/buying power to deploy
    "sector_or_cluster_crowded",  # concentration against something already held
    "better_use_of_the_slot",     # ranked below a name that was taken instead
    "already_sized_correctly",    # held, and the current size is the right one
    # the escape hatch — detail is what carries it
    "other",
)


class CandidateRejection(LLMOutputModel):
    """One candidate the portfolio manager was shown and chose NOT to target.

    Why this exists (board item 133, 2026-09-18). The PM returned a list of
    TARGETS and nothing else, so `DecisionStage` recorded every analysed
    candidate missing from that list as
    `portfolio_manager|omitted|candidate_not_selected_for_target`. The seat
    was never ASKED why it dropped a name, so there was no per-candidate
    reason and there could not be one — the absence of a reason WAS the
    reason, identically, for every name and every session. On 2026-09-18 the
    jam detector fired on exactly that: two sessions, three different
    candidates, one unvarying key. It could not have reported anything else,
    which is what made it useless as a diagnosis.

    `code` is the comparable identity; `detail` is what a person reads.
    Both are kept — the code alone does not tell the owner what happened to
    HIS candidate, and the prose alone cannot be compared across sessions.

    Fail-OPEN on shape, exactly as `SymbolRejection` is and for the same
    reason: a rejection lost to a formatting slip turns back into the silent
    omission this model exists to end. An unrecognised code becomes `other`
    with the original spelling preserved in `detail`; a missing detail
    becomes a stated absence. Only an entry naming no recoverable symbol is
    dropped, because there is nothing to file it against.
    """

    symbol: str
    code: str = Field(default="other")
    detail: str = Field(default="", max_length=600)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values):
        _absent = (
            "the portfolio manager named no reason beyond dropping the name"
        )
        if isinstance(values, str):
            return {"symbol": values, "code": "other", "detail": _absent}
        if not isinstance(values, dict):
            return values
        values = dict(values)
        # Tolerate the two spellings a model actually reaches for.
        for alias in ("reason_code", "rejection_code"):
            if not values.get("code") and values.get(alias):
                values["code"] = values[alias]
        for alias in ("reason", "explanation", "why"):
            if not values.get("detail") and values.get(alias):
                values["detail"] = values[alias]
        raw_code = str(values.get("code") or "").strip().lower().replace(" ", "_")
        detail = values.get("detail")
        detail = detail.strip() if isinstance(detail, str) else ""
        if raw_code not in CANDIDATE_REJECTION_CODES:
            if raw_code:
                # Never paraphrase an unknown code into a known one — that
                # would invent a cause. Keep the spelling where a person can
                # read it and let it count as `other` when compared.
                detail = (
                    f"[unrecognised reason code '{raw_code}'] {detail}".strip()
                )
            raw_code = "other"
        values["code"] = raw_code
        values["detail"] = detail or _absent
        return values

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return _normalize_symbol(v)


class PortfolioDecision(LLMOutputModel):
    reasoning_chain: ReasoningChain
    # Phase 2 output: PM emits intent (target weights), not orders.
    targets: list[TargetPosition] = Field(default_factory=list)
    #: Every candidate the seat was shown and is NOT targeting, with the
    #: named ground it dropped it on (board item 133, 2026-09-18). A target
    #: or a rejection — the seat must account for each name one way or the
    #: other. Empty here is not an error at PARSE time (an old stored row
    #: must still load, and a validation failure would fail the whole
    #: decision closed over a bookkeeping field). The accounting is enforced
    #: downstream in `DecisionStage`, in the desk's standing heal order:
    #: mechanical heal, one bounded re-ask, then a durable per-symbol
    #: refusal — the same posture `missing_open_falsifier` documents.
    rejections: list[CandidateRejection] = Field(default_factory=list)
    # Phase 2 derived: populated by PortfolioConstructor AFTER the LLM returns.
    # Downstream stages (hard risk filter, RM review, execution) read this.
    # PM must never fill it directly — the LLM output is validated with
    # `decisions` empty; the pipeline injects constructor output before
    # handing the object off to downstream stages.
    # `SkipJsonSchema`: kept off the rendered response_format entirely. Strict
    # structured output made this field REQUIRED, so the seat was ordered to
    # emit a full order book (entry_price / stop_loss / take_profit per name)
    # that `PortfolioManagerAgent.validate_grounding` then refuses outright —
    # "portfolio manager supplied concrete decisions; only grounded targets
    # may cross the PM boundary" discards every target and the whole book.
    # The prompt never mentions the field, so there was nothing telling the
    # seat to leave it empty.
    decisions: Annotated[list[TradeDecision], SkipJsonSchema()] = Field(default_factory=list)
    #: Symbols the PM proposed that the deterministic constructor DROPPED
    #: (unmeasurable range reward:risk, no readable structure, no valid
    #: stop, ... — no reward:risk floor since 2026-09-11). Set by
    #: the pipeline after `construct_orders`, never by the LLM.
    #:
    #: Exists because PM writes its `reasoning_chain` BEFORE the constructor
    #: runs, and that narrative is rendered to the Risk Manager verbatim. When
    #: the constructor silently removed a trade, the RM saw a story arguing for
    #: symbols absent from the order list and — correctly, given what it was
    #: shown — vetoed the WHOLE plan as incoherent. On 2026-08-31 that killed
    #: two trades the RM had just called valid ("While COP and V are valid, the
    #: plan as presented is not internally consistent"). Telling the RM what
    #: was removed, and that removal was deterministic, is what makes the
    #: remaining plan legible. Same reasoning as the constructor's existing
    #: `cap_note` provenance, which solved this once already for allocation
    #: caps (portfolio_constructor.py ~947).
    # `SkipJsonSchema` for the same reason as `decisions` above: set by the
    # pipeline after `construct_orders`, never by the LLM, so it has no place
    # in the schema the LLM is handed.
    constructor_dropped: Annotated[list[str], SkipJsonSchema()] = Field(default_factory=list)
    portfolio_view: str


class RiskModification(LLMOutputModel):
    symbol: str
    field: str
    original_value: float
    new_value: float
    reason: str

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        # Same normalization as `SymbolRejection._normalize` — 2026-09-03,
        # item 26. Without it, `_apply_risk_modifications` (src/pipeline.py)
        # matched `mod.symbol` against `decision.symbol` case-sensitively,
        # silently dropping a modification whose case differed (fixed at
        # that one comparison site the same day) — but a second, independent
        # comparison (`decision.symbol in modified_symbols`,
        # src/pipeline_stages.py, deciding whether a symbol's funnel outcome
        # reads "modified" or "approved") read the SAME unnormalized field
        # and would have kept silently mismatching. Normalizing at the model
        # boundary closes both call sites (and any future one) at once,
        # rather than patching each comparison site individually.
        return _normalize_symbol(v)


def _normalize_rejected_symbols_field(values):
    """Coerce the container shapes an LLM emits for `rejected_symbols` into
    the list of objects the schema declares.

    Same fail-open logic as `SymbolRejection._coerce_shorthand`: losing a
    refusal to a container-shape slip means a name the risk manager refused
    goes on to trade, so the shapes that unambiguously carry the same
    information are accepted —

        "XLE"                        -> [{"symbol": "XLE"}]
        "XLE, CHPX"                  -> [{"symbol": "XLE"}, {"symbol": "CHPX"}]
        {"symbol": "XLE", ...}       -> [ {"symbol": "XLE", ...} ]
        {"XLE": "R/R 1.18 < 1.5"}    -> [{"symbol": "XLE", "reason": "..."}]

    Any other shape (a number, a bool) is left exactly as-is so Pydantic
    raises on it — `rejected_symbols` is decision-bearing, so that failure
    correctly fails the whole verdict closed rather than silently trading a
    refused name.
    """
    if not isinstance(values, dict):
        return values
    raw = values.get("rejected_symbols")
    if raw is None or isinstance(raw, list):
        return values
    values = dict(values)
    if isinstance(raw, str):
        values["rejected_symbols"] = [
            {"symbol": part} for part in raw.split(",") if part.strip()
        ]
    elif isinstance(raw, dict):
        if "symbol" in raw:
            values["rejected_symbols"] = [raw]
        else:
            values["rejected_symbols"] = [
                {"symbol": sym, "reason": reason if isinstance(reason, str) else None}
                for sym, reason in raw.items()
            ]
    return values


class SymbolRejection(LLMOutputModel):
    """One symbol refused on its own merits, without touching the rest of
    the plan. The per-TRADE lane of the risk verdict.

    Why this exists (spec Phase 10.1). `RiskVerdict.approved` is one bool
    for the whole plan, and `RiskModification` can retune a symbol's fields
    but cannot refuse one. So a single failing leg killed every other leg:
    on run `run-64290730` (2026-09-01 morning) the risk manager rejected the
    whole plan citing XLE alone — constructed R/R 1.18, under the 1.5 floor —
    and CHPX died with it at R/R 3.03, different sector, unrelated thesis.
    Zero trades.

    The governing principle, in the owner's words: *"The batch is arbitrary —
    it is whatever happened to be proposed in one run. Judging a trade against
    its accidental co-passengers makes no sense. Judge it against what the
    account actually holds."* A per-SYMBOL failure (an R/R breach on one name,
    an event-risk flag on one name) refuses that name and nothing else. A
    BOOK-level failure (a correlation cluster, total exposure, drawdown state)
    is a property of the whole account and still refuses everything, via
    `approved=false` — see the field comment on `RiskVerdict.rejected_symbols`.

    `reason` is not optional prose: it is the per-symbol audit trail, written
    to `specialist_evidence` (kind=`rejection`, scope=`symbol`) and to the
    symbol's `pipeline_event`, so "why was this name refused" is answerable
    per name rather than only per run.
    """
    symbol: str
    reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _coerce_shorthand(cls, values):
        """Accept the two shorthands an LLM actually emits, and NEVER lose a
        refusal to a formatting slip.

        A dropped rejection is fail-OPEN — a name the risk manager refused
        would trade — so this normalizes rather than discards: a bare
        `"XLE"` string becomes a rejection with a stated absent reason, and
        a missing/blank `reason` on an otherwise well-formed entry becomes
        the same. An entry naming NO recoverable symbol is deliberately left
        to fail validation: the verdict then fails closed as a whole (see
        `RiskManagerAgent._DECISION_FIELDS`), because we know a refusal was
        intended and cannot tell which name it was for.
        """
        _absent = "risk manager refused this symbol without stating a reason"
        if isinstance(values, str):
            return {"symbol": values, "reason": _absent}
        if isinstance(values, dict):
            values = dict(values)
            reason = values.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                values["reason"] = _absent
        return values

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return _normalize_symbol(v)


#: The one label a risk verdict carries, shared by both verdict shapes.
#: Extracted so the exit-path verdict cannot drift a category out of step with
#: the morning one — `portfolio_manager` reads this field's recent history to
#: self-calibrate and a category it does not know is a silently dropped signal.
RiskReasonCategory = Literal[
    "clean",             # approved untouched, no mods
    "oversized",         # sizing too aggressive vs conviction
    "rr_fail",           # legacy label: a range BUY cut/refused with reward:risk as a named factor (no universal floor since 2026-09-11; never applies to a breakout)
    "concentration",     # sector / single-name too heavy
    "correlation_risk",  # theme/factor clustering flagged
    "event_risk",        # pre-earnings / FOMC / macro event volatility
    "macro_misalign",    # plan direction contradicts the macro read (legacy: exposure vs a macro target, removed 2026-09-17)
    "data_degraded",     # multiple upstream sources failed
    "signal_fidelity",   # PM contradicts TechAnalyst without explanation
    "other",             # doesn't fit the above
]


#: The reason categories that on their OWN mark a whole-plan veto
#: (`approved=False` refusing every leg) as genuinely BOOK-WIDE. Owner ruling
#: 2026-09-24, closing the item-162 harm: hard limits are enforced by CODE at
#: the deterministic gate BEFORE the seat runs (`_filter_hard_risk_decisions`),
#: so the AI seat's whole-plan veto is a judgement layer sitting on a book that
#: already cleared every hard limit. Over a mere ADVISORY or single-name
#: concern the seat may RESIZE (`modifications`, `scale_all_buys`) or refuse
#: ONE name (`rejected_symbols`) — it may NOT nuke the whole batch.
#:
#: This set is only HALF the honor test — see `RiskStage` for the whole of it.
#: The decisive signal is SCOPE: a veto whose seat named at least one droppable
#: symbol IS an actionable per-symbol remedy and is downgraded to dropping
#: exactly those names, UNLESS the category here says the danger is book-wide
#: (a cross-book correlation cluster, which naming individual names cannot
#: fix). A veto with NO per-symbol remedy is honored in full whatever its
#: category, so aggregate/total-exposure and whole-plan-incoherence vetoes —
#: which have no per-symbol remedy — are honored by scope, not by a category
#: here.
#:
#: `concentration` is deliberately NOT in this set. The enum and the seat's
#: prompt both define it as "sector / SINGLE-NAME too heavy" and apply it to
#: one order's weight; whitelisting it would let a single overweight name veto
#: the whole batch — the exact item-162 harm. A one-name concentration concern
#: is a `rejected_symbols` entry; an aggregate-exposure concern is a batch veto
#: with an EMPTY `rejected_symbols`, honored by the no-remedy scope rule.
#:
#: Membership is checked by EXACT equality, never substring, and lives here
#: beside `RiskReasonCategory` so the one enum and the one whitelist cannot
#: drift apart. `config/prompts/risk_manager.md` carries a machine-checkable
#: mirror of this set (see `tests/test_risk_verdict_per_symbol.py`
#: `test_prompt_and_code_agree_on_book_veto_categories`).
BOOK_LEVEL_VETO_CATEGORIES: frozenset[str] = frozenset({
    "correlation_risk",  # theme/factor cluster across the batch — book-wide
})


class _PerSymbolRejections:
    """`rejections_by_symbol()` for both risk verdict shapes.

    A plain mixin, deliberately carrying NO annotations: pydantic collects
    field annotations from every base, so declaring `rejected_symbols` here
    would reorder the fields of `RiskVerdict`, which is serialised into
    archived agent logs.
    """

    def rejections_by_symbol(self) -> dict[str, str]:
        """`{SYMBOL: reason}` for every per-symbol refusal in this verdict.

        First entry wins on a duplicated symbol — two reasons for refusing
        the same name still refuse it once, and the first is the one the
        audit trail carries.
        """
        out: dict[str, str] = {}
        for rejection in self.rejected_symbols:
            out.setdefault(rejection.symbol, rejection.reason)
        return out


class RiskReasoningChain(LLMOutputModel):
    """6-step CoT for the risk manager — forces audit trail on the last gate.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.

    MORNING PLAN path only. The exit review uses `ExitRiskReasoningChain`,
    which is this chain minus the three steps the exit prompt itself stands
    down or inverts — see that class.
    """
    rr_audit: str = Field(min_length=1)             # setup-aware since 2026-09-11: breakouts carry no R/R judgement; a range trade's real ratio is an input, not a floor
    signal_fidelity: str = Field(min_length=1)      # does PM's action align with Tech/Macro/News? silent contradictions?
    correlation_check: str = Field(min_length=1)    # any hidden cluster / factor concentration across decisions?
    event_risk: str = Field(min_length=1)           # earnings / FOMC / macro events in the coming 3 days affecting these names?
    sizing_sanity: str = Field(min_length=1)        # is size proportional to conviction and R/R? any outsized bet?
    overall: str = Field(min_length=1)              # final synthesis and why approved/rejected/modified


class ExitRiskReasoningChain(LLMOutputModel):
    """The risk seat's chain on the EXIT-REVIEW path.

    Same six steps as `RiskReasoningChain`, but three of them are no longer
    MANDATORY, because the exit prompt itself already tells the seat they do
    not apply here (`src/agents/risk_review_mode._EXIT_REVIEW_HEADER`), and a
    `min_length=1` field is a demand for an answer:

    - `rr_audit` — checklist 2 is stood down. An exit has no entry geometry:
      `_risk_review_exits` sends entry, stop and target as `0.0` because there
      is no such thing to send. There is no ratio to audit.
    - `sizing_sanity` — checklist 5 is stood down. No BUY or SHORT reaches
      this path, and the % on an exit is the position reviewer's call on its
      own position.
    - `event_risk` — checklist 4 INVERTS here. "A binary event inside the
      window, so downsize or reject" was written for an ENTRY, where refusing
      carries LESS risk through the event. On an exit, refusing carries the
      position THROUGH it. Compelling an answer to a question whose standing
      instruction points the wrong way is not a safeguard; it produced the one
      measured harm on this path. Archived row 330 (2026-09-01, `close-`
      `0e9129f1`) answered "next earnings dates were NOT FETCHED this run" and
      then set `reason_category: "data_degraded"` on that basis — a label
      `portfolio_manager` reads back to self-calibrate. Optional here means
      the seat may still report the dates it was given; it is no longer forced
      to manufacture a paragraph about them.

    `signal_fidelity`, `correlation_check` and `overall` stay mandatory: all
    three are live questions on an exit, and `overall` is the audit trail.

    Nothing here touches the morning BUY path — `RiskReasoningChain` is
    unchanged and all six of its fields remain mandatory.
    """
    rr_audit: str = ""                              # stood down on this path (no entry geometry)
    signal_fidelity: str = Field(min_length=1)      # does the reviewer's exit align with News/Macro? silent contradictions?
    correlation_check: str = Field(min_length=1)    # what closing these leaves the book concentrated in
    event_risk: str = ""                            # instruction inverts on this path; report, never compelled
    sizing_sanity: str = ""                         # stood down on this path (nothing to size)
    overall: str = Field(min_length=1)              # final synthesis and why approved/refused


class ExitRiskVerdict(_PerSymbolRejections, LLMOutputModel):
    """The risk seat's verdict on the EXIT-REVIEW path.

    `RiskVerdict` minus the two levers that do nothing here. `modifications`
    and `scale_all_buys` are applied ONLY by `_apply_risk_modifications`,
    which is called ONLY from the morning `RiskStage`
    (`src/pipeline_stages.py`); `_risk_review_exits` returns a veto SET and
    reads neither. They were emitted, parsed, stored and discarded. A field
    the seat is asked to fill and no code consumes is not a harmless extra —
    it is an instruction to spend judgement on a lever that is not connected.

    Refusal is the whole lever here: `approved=False` refuses every exit in
    the batch (the book is what failed), `rejected_symbols` refuses one name
    and lets the rest through. Per doctrine a refusal DROPS the exit from the
    batch with a durable per-symbol reason; nothing on this path zeroes a
    target.
    """
    approved: bool
    reasoning_chain: ExitRiskReasoningChain
    # Same semantics as on `RiskVerdict`: each entry kills exactly one exit
    # and leaves the others standing. The `reason` is the per-symbol audit
    # trail written to `specialist_evidence`.
    rejected_symbols: list[SymbolRejection] = []
    reason_category: RiskReasonCategory = "clean"
    reasoning: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_rejected_symbols_field(values)
        return _normalize_enum_case_fields(values, lower_fields=("reason_category",))


class RiskVerdict(_PerSymbolRejections, LLMOutputModel):
    # BOOK-level verdict. `approved=False` still refuses the ENTIRE plan and
    # always will: correlation clusters, total exposure and drawdown state are
    # properties of the whole account, so when the BOOK is what fails, killing
    # every leg is the correct answer. What changed in Phase 10.1 is only the
    # GRANULARITY available for the other kind of failure — see
    # `rejected_symbols`. No threshold moved.
    approved: bool
    reasoning_chain: RiskReasoningChain
    modifications: list[RiskModification] = []
    # PER-SYMBOL refusal. Each entry kills exactly one leg and leaves every
    # other leg standing; the survivors then go through `modifications`,
    # `scale_all_buys` and the deterministic hard-risk gate unchanged.
    #
    # This is the third rung of a four-rung ladder, narrowest first:
    #   modifications    — retune one symbol's fields (size, stop, target)
    #   rejected_symbols — refuse one symbol outright, book unaffected
    #   scale_all_buys   — size the whole entry side down, refuse nothing
    #   approved=False   — the book itself is unsound; nothing trades
    #
    # Book-level always wins: `approved=False` is evaluated first, so a
    # verdict carrying both refuses everything regardless of what this list
    # says. An entry naming a symbol not in the plan is a no-op, logged.
    #
    # Default-empty by design — every historical verdict, and every verdict
    # from a model that never emits the field, replays with byte-identical
    # behaviour.
    rejected_symbols: list[SymbolRejection] = []
    # Portfolio-level size control. Multiplies every BUY decision's allocation_pct after
    # per-symbol modifications are applied. 1.0 = no change; 0.5 = half all buys; 0.0
    # effectively kills BUY side while leaving SELL/HOLD/TRAIL intact.
    scale_all_buys: float = Field(default=1.0, ge=0.0, le=1.0)
    # Categorized reason for any modification / scaling. PM reads the recent
    # history of this field to self-calibrate in a targeted way: repeated
    # `oversized` means cut base allocations; repeated `rr_fail` means trust
    # TA's R/R math more literally; etc. One label per verdict.
    reason_category: RiskReasonCategory = "clean"
    reasoning: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_rejected_symbols_field(values)
        return _normalize_enum_case_fields(values, lower_fields=("reason_category",))


class MacroObservation(LLMOutputModel):
    indicator: str
    reading: str
    interpretation: str


# yfinance sector taxonomy (matches what broker._get_sector returns).
# "Broad" covers index ETFs (SPY/QQQ/IWM/DIA) that have no single sector tag.
_ALLOWED_SECTORS = (
    "Technology", "Financial Services", "Healthcare", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Industrials", "Communication Services",
    "Utilities", "Basic Materials", "Real Estate", "Broad",
)

# Common LLM-emitted aliases → canonical name. Applied before the Literal check
# so a single bad label doesn't discard the whole MacroAnalysis.
_SECTOR_ALIASES = {
    "tech": "Technology",
    "technology": "Technology",
    "financials": "Financial Services",
    "financial": "Financial Services",
    "banks": "Financial Services",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "materials": "Basic Materials",
    "comm services": "Communication Services",
    "communication": "Communication Services",
    "telecom": "Communication Services",
    "reits": "Real Estate",
    "real-estate": "Real Estate",
    "index": "Broad",
    "broad market": "Broad",
    "etf": "Broad",
}


# The macro analyst speaks TILTS (overweight/underweight); every consumer of a
# sector stance — MacroStore's persisted snapshot, the evening thesis-health
# block, `PositionSnapshot.macro_sector_tailwind` below, and the PM's evidence
# registry — speaks DIRECTIONS (bullish/bearish). One macro view described in
# two vocabularies is how a provenance mismatch gets debugged twice, so the
# translation lives here, next to the Literal that defines the tilt side of it,
# and every consumer imports it rather than re-spelling the pairs.
SECTOR_STANCE_TO_DIRECTION: dict[str, str] = {
    "overweight": "bullish",
    "neutral": "neutral",
    "underweight": "bearish",
}

# Directions are idempotent under the map: a stance that already arrived
# normalized (MacroStore's shape) must survive a second pass unchanged.
SECTOR_DIRECTIONS: frozenset[str] = frozenset(SECTOR_STANCE_TO_DIRECTION.values())


def normalize_sector_stance(value) -> str | None:
    """overweight|underweight|neutral (or a direction already) → direction.

    Returns None for anything unrecognized so callers can drop it rather
    than propagate a stance no validator will accept.
    """
    stance = str(value or "").strip().lower()
    if stance in SECTOR_DIRECTIONS:
        return stance
    return SECTOR_STANCE_TO_DIRECTION.get(stance)


class MacroSectorGuidance(LLMOutputModel):
    sector: Literal[
        "Technology", "Financial Services", "Healthcare", "Consumer Cyclical",
        "Consumer Defensive", "Energy", "Industrials", "Communication Services",
        "Utilities", "Basic Materials", "Real Estate", "Broad",
    ]
    stance: Literal["overweight", "neutral", "underweight"]
    reason: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # `sector` is canonicalized via _SECTOR_ALIASES in MacroAnalysis's
        # _sanitize_sector_guidance; only `stance` needs case-folding.
        return _normalize_enum_case_fields(values, lower_fields=("stance",))


class MacroPositionGuidance(LLMOutputModel):
    """Macro's read on which way the book should LEAN (net long vs net
    short, and why) — never how much of it sits idle.

    Owner mandate 2026-09-17: the desk is fully invested, always. The
    invested target is `src.risk.rules.DESK_INVESTED_TARGET_PCT`, a fixed
    mandate, not a macro output. `target_invested_pct` and
    `cash_recommendation_pct` were deleted from this model that day so no
    seat can be handed a macro reason to hold cash. Snapshots persisted
    before then still carry both keys; unknown keys are ignored, so they
    still parse and the numbers simply stop reaching anyone.
    """
    reasoning: str


class MacroReasoningChain(LLMOutputModel):
    """Six-step CoT, one field per step — forces the LLM to walk each stage.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.
    """
    volatility_analysis: str = Field(min_length=1)        # VIX regime, trend, term structure if inferable
    yield_curve_analysis: str = Field(min_length=1)       # 2Y/10Y level, spread, inversion trajectory
    monetary_policy_analysis: str = Field(min_length=1)   # Fed funds (DFF) level + direction
    inflation_labor_credit: str = Field(min_length=1)     # CPI + UNRATE + HY OAS combined read
    cross_signal_synthesis: str = Field(min_length=1)     # How the above reinforce or contradict each other
    sector_implications: str = Field(min_length=1)        # What this means for sector tilts


class MacroAnalysis(LLMOutputModel):
    reasoning_chain: MacroReasoningChain
    regime: Literal["risk-on", "risk-off", "neutral", "transitional"]
    confidence: Literal["high", "medium", "low"]
    equity_outlook: Literal["bullish", "bearish", "neutral"]
    regime_shift: bool = False
    shift_reason: str = ""
    key_observations: list[MacroObservation] = []
    sector_guidance: list[MacroSectorGuidance] = []
    risk_factors: list[str] = []
    position_guidance: MacroPositionGuidance
    bull_triggers: list[str] = []
    bear_triggers: list[str] = []
    alignment_with_news: str = ""
    summary: str
    # Phase 9 (§9.1): sector leaders Macro wants Technical to look at when
    # a regime turns. Default [] so a MacroAnalysis persisted before this
    # field existed (macro_store snapshots, replayed decisions) still
    # parses unchanged. Bounded and deduped by the pipeline, not here —
    # see src/nominations.py.
    nominations: list[Nomination] = []

    # DELETED 2026-09-13 (retired item 31): `_MAGNITUDE_BY_CONFIDENCE`
    # ({high 0.75, medium 0.5, low 0.25}) and `_REGIME_SHIFT_BONUS` (0.25).
    # The first was a table on `confidence`, which is the very field this
    # verdict hands to `conviction` — so `score_verdict`'s two-signal
    # composite was counting macro's confidence twice, at an unsourced
    # spacing. The second was an unsourced constant on top of it. Magnitude
    # is now `NO_STATED_STRENGTH` (0.0); `regime_shift`/`shift_reason`
    # still reach the reader through `invalidation` below, where they are the
    # analyst's own words rather than a number nobody derived.

    def to_verdict(self, symbol: str, *, sector: str | None = None) -> "AnalystVerdict":
        """This read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion, mirroring
        `TechAnalysisResult.to_verdict` — see that method for the pattern
        this follows.

        `MacroAnalysis` is market-wide, not per-symbol (no `symbol` field
        on this model — see the evidence-registry seat-name comment
        threaded through `PortfolioManagerAgent.build_evidence_registry`,
        which already applies one macro read to many symbols). The caller
        supplies which symbol this verdict is being cast for, and — as of
        2026-09-13 — which SECTOR that symbol belongs to.

        direction    — `sector`'s own stance from `sector_guidance` when this
                       read stated one for it; otherwise `equity_outlook`.

                       **2026-09-13, retired item 31.** This used to be
                       `equity_outlook` for every symbol, while
                       `build_evidence_registry` — the SAME macro read, in
                       the same prompt — already resolved a per-symbol stance
                       from `sector_guidance` and only fell back to the broad
                       outlook when the sector had no row. So the desk could
                       tell the PM "macro is bearish energy" in the evidence
                       registry and simultaneously rank an energy name on a
                       bullish broad read. One belief, two answers.

                       The resolution here is deliberately the registry's
                       own, not a second rule: the matching rows' stances go
                       through the identical `collapse_stances` reduction
                       (`src/quantities.py`, the one definition both call),
                       and the result is translated tilt -> direction by
                       `normalize_sector_stance` — the module-level map that
                       exists precisely so overweight/underweight is not
                       respelled per consumer. An unresolved split across
                       rows ("mixed") normalizes to None and is treated as
                       NEUTRAL, matching the registry, where "mixed" fails
                       `stance_is_aligned` and supports nothing. Nothing is
                       invented and no number is introduced.

                       The objection this method used to record — that
                       `MacroAnalysis` "carries one conviction/evidence/
                       invalidation set for its whole read, so there is
                       nothing sector-specific to attach" — is only half
                       true, and the true half does not block this. Evidence
                       IS sector-specific: the matching row's own `reason`,
                       already on the model, is surfaced first and labelled
                       as the stance that decided the direction. Conviction
                       is not, so `confidence` is still used unchanged — it
                       is the only confidence this seat states, it is what
                       the broad read used too, and substituting a
                       sector-specific one would mean inventing it.
        conviction   — `confidence` verbatim; already high/medium/low.
        magnitude    — `NO_STATED_STRENGTH` (0.0), directional or not. See
                       that constant for why the previous confidence-keyed
                       table and regime-shift bonus were deleted rather than
                       re-derived, and why nothing was borrowed in their
                       place. `regime_shift` in particular changes nothing
                       here — `AnalystVerdict` refuses a neutral verdict with
                       nonzero magnitude anyway, and a "neutral, but
                       shifting" read is a contradiction in terms this
                       method does not try to resolve silently. This seat
                       reaches the ranking through its weighted conviction.
        evidence     — the deciding `sector_guidance` row first when a sector
                       stance was applied (labelled `sector_stance:<sector>`,
                       so a reader can see WHY this symbol's direction
                       differs from the broad one), then `key_observations`
                       (indicator/reading/interpretation, one VerdictEvidence
                       each), the full `sector_guidance` list (sector +
                       stance + reason), and `risk_factors` (one per item).
                       All qualitative (`text=`); MacroAnalysis carries no
                       evidence-shaped numbers to attach as `value=`.
        invalidation — `shift_reason` when `regime_shift` is True and the
                       reason is non-empty: the stated condition already IS
                       the falsifier for a regime call. Otherwise, mirroring
                       Technical's "fall back to the analyst's own stated
                       falsifier" pattern: for a bullish call, the first
                       `bear_triggers` entry (what would prove it wrong); for
                       a bearish call, the first `bull_triggers` entry.
                       UNLIKE Technical, when direction is not neutral and
                       none of the above is available (no shift_reason, no
                       trigger on the falsifying side), a generic fallback
                       string is used rather than leaving this blank —
                       `AnalystVerdict` REQUIRES a non-empty invalidation for
                       any non-neutral call (see its
                       `_a_call_must_be_falsifiable_and_backed` validator) and
                       raises `ValidationError` otherwise, so an empty string
                       is only ever valid here for a neutral outlook.
        """
        # The sector rows this read stated for the symbol's own sector, if
        # the caller supplied one. Matched on the canonical `sector` Literal
        # (`_sanitize_sector_guidance` has already aliased it), case- and
        # whitespace-insensitively, the same way `build_evidence_registry`
        # keys its own lookup.
        wanted = (sector or "").strip().lower()
        sector_rows = [
            row for row in self.sector_guidance
            if wanted and row.sector.strip().lower() == wanted
        ]
        sector_direction = (
            normalize_sector_stance(collapse_stances(row.stance for row in sector_rows))
            if sector_rows else None
        )
        if sector_rows and sector_direction is None:
            # `collapse_stances` returned "mixed" — the sector's own rows
            # disagree. The registry treats that as supporting nothing, so
            # this does too, rather than quietly falling back to the broad
            # read the sector rows were specifically contradicting.
            sector_direction = "neutral"

        direction = sector_direction or self.equity_outlook
        # 0.0 either way — see `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH

        evidence: list[VerdictEvidence] = []
        for row in sector_rows:
            evidence.append(VerdictEvidence(
                label=f"sector_stance:{row.sector}",
                text=(
                    f"{row.stance} — {row.reason} (this sector stance sets "
                    f"{symbol}'s macro direction; broad equity_outlook is "
                    f"{self.equity_outlook})"
                ),
            ))
        for obs in self.key_observations:
            evidence.append(VerdictEvidence(
                label=obs.indicator,
                text=f"{obs.reading} — {obs.interpretation}",
            ))
        for row in self.sector_guidance:
            evidence.append(VerdictEvidence(
                label=f"sector:{row.sector}",
                text=f"{row.stance} — {row.reason}",
            ))
        for i, factor in enumerate(self.risk_factors):
            evidence.append(VerdictEvidence(label=f"risk_factor_{i}", text=factor))
        if not evidence and direction != "neutral":
            # `key_observations`/`sector_guidance`/`risk_factors` are all
            # `= []` defaults — an actionable read that populated none of
            # them still has to satisfy `AnalystVerdict`'s "a directional
            # call must cite at least one piece of evidence" rule. The
            # reasoning chain is mandatory (`min_length=1` on every field),
            # so it is always available as a last-resort citation — nothing
            # is invented, this is the analyst's own synthesis restated.
            evidence.append(VerdictEvidence(
                label="cross_signal_synthesis",
                text=self.reasoning_chain.cross_signal_synthesis,
            ))

        invalidation = ""
        if direction != "neutral":
            shift_reason = (self.shift_reason or "").strip()
            if self.regime_shift and shift_reason:
                invalidation = shift_reason
            elif direction == "bullish" and self.bear_triggers:
                invalidation = self.bear_triggers[0]
            elif direction == "bearish" and self.bull_triggers:
                invalidation = self.bull_triggers[0]
            else:
                # No stated falsifier anywhere on the read. Technical can
                # fall back to its own hard stop; macro has no analogous
                # always-present number, so the fallback is a generic but
                # honest statement rather than a blank field that would
                # fail `AnalystVerdict`'s non-neutral-invalidation rule.
                invalidation = (
                    f"equity_outlook reverses from {direction} "
                    "(macro analyst stated no explicit trigger)"
                )

        return AnalystVerdict(
            seat="macro",
            symbol=symbol,
            direction=direction,
            magnitude=magnitude,
            conviction=self.confidence,
            evidence=evidence,
            invalidation=invalidation,
        )

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # Three top-level enums on MacroAnalysis are LLM-emitted lowercase.
        # Runs before _sanitize_sector_guidance and the Literal check.
        return _normalize_enum_case_fields(
            values,
            lower_fields=("regime", "confidence", "equity_outlook"),
        )

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_sector_guidance(cls, values):
        """Map aliases, drop unknown sectors — preserves the rest of the analysis.

        Previously a single bad sector name (e.g. "Financials" instead of
        "Financial Services") rejected the whole MacroAnalysis and left PM blind.
        """
        if not isinstance(values, dict):
            return values
        sg = values.get("sector_guidance")
        # MacroStore persists {sector: bullish|neutral|bearish}. Live LLM
        # output is a list of {sector, stance, reason}. A dict used to be
        # left untouched, then MacroAnalysis.model_validate raised
        # (measured 2026-09-16, two intra ticks). Coerce the dict to the
        # list shape here — mechanical, no invented reasons — so a stored
        # snapshot and a live dict-shaped answer both parse. Missing
        # reasoning_chain is still a ValidationError: we do not invent it.
        if isinstance(sg, dict):
            converted: list[dict] = []
            reverse = {
                "bullish": "overweight",
                "bearish": "underweight",
                "neutral": "neutral",
            }
            for sector, direction in sg.items():
                stance = reverse.get(str(direction or "").strip().lower())
                if stance is None:
                    stance = str(direction or "").strip().lower()
                converted.append({
                    "sector": sector,
                    "stance": stance,
                    "reason": "",
                })
            values = dict(values)
            values["sector_guidance"] = converted
            sg = converted
        if not isinstance(sg, list):
            return values
        cleaned: list[dict] = []
        for item in sg:
            if not isinstance(item, dict):
                continue
            sec = item.get("sector")
            if not isinstance(sec, str):
                continue
            canon = _SECTOR_ALIASES.get(sec.strip().lower(), sec.strip())
            if canon in _ALLOWED_SECTORS:
                new_item = dict(item)
                new_item["sector"] = canon
                cleaned.append(new_item)
            # else: silently drop — we'd rather lose one guidance row than the whole analysis
        values["sector_guidance"] = cleaned
        return values


class MacroNarrative(LLMOutputModel):
    last_updated: str
    era_themes: list[str] = Field(min_length=1)
    current_regime: str = Field(min_length=5)
    key_state_tracker: dict[str, str] = {}

    @field_validator("last_updated")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class StateChange(LLMOutputModel):
    event: str
    previous_state: str
    new_state: str
    market_impact: str
    affected_symbols: list[str] = []
    conviction: Literal["high", "medium", "low"]
    # Phase 13 catalyst-gate fix (2026-09-03): per-symbol direction for
    # THIS state change, keyed by symbols named in `affected_symbols`.
    # NOT a single scalar — `market_impact` is free text that routinely
    # names OPPOSITE directions for different symbols in the same row
    # (see the ceasefire/oil example in config/prompts/news_analyst.md:
    # "Bullish for consumer discretionary and airlines, bearish for
    # energy" over one row naming both XLY and XLE names). A scalar
    # direction would misrepresent exactly the rows most likely to
    # matter for this.
    #
    # Populated by the SAME news_analyst LLM call that already produces
    # `StockNewsItem.sentiment` for individual stock items — no new LLM
    # call, no new analyst seat. A symbol absent from this dict has no
    # recorded direction; `PortfolioManagerAgent._catalyst_cites_state_
    # change` (src/agents/portfolio_manager.py) treats that the same as
    # an explicit "neutral": it does not qualify for the sub-floor
    # catalyst exception. Fail closed, matching the rest of that gate.
    symbol_direction: dict[str, Literal["bullish", "bearish", "neutral"]] = {}

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_enum_case_fields(values, lower_fields=("conviction",))
        if isinstance(values, dict):
            raw = values.get("symbol_direction")
            if isinstance(raw, dict):
                cleaned: dict[str, str] = {}
                for sym, direction in raw.items():
                    if not isinstance(sym, str) or not isinstance(direction, str):
                        continue
                    d = direction.strip().lower()
                    # Unrecognized directions are DROPPED, not raised —
                    # one malformed entry must narrow what can be cited,
                    # never crash the whole news report (same posture as
                    # `_normalize_enum_case_fields` above). A dropped
                    # entry is indistinguishable from "no direction
                    # recorded" downstream, which is exactly the fail-
                    # closed behavior wanted.
                    if d not in ("bullish", "bearish", "neutral"):
                        continue
                    s = sym.strip().upper()
                    if s:
                        cleaned[s] = d
                values = {**values, "symbol_direction": cleaned}
        return values


class StockNewsItem(LLMOutputModel):
    headline: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    conviction: Literal["high", "medium", "low"]
    impact_summary: str

    @field_validator("headline")
    @classmethod
    def require_headline(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("headline cannot be empty")
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values, lower_fields=("sentiment", "conviction"),
        )


#: `news_verdict_for_symbol`'s conviction -> `AnalystVerdict.magnitude` map.
#: Judgment call (flagged for review, Phase 13 §13.3 posture — start equal,
#: adjust only on out-of-sample proof): a `StockNewsItem` carries no numeric
#: field at all, only a three-rung conviction, so unlike
#: `RATING_MAGNITUDE` (which has a true neutral rung at 0.0 to anchor
#: against) there is nothing to equally space AROUND. Spacing the three
#: rungs equally across (0, 1] — never landing on 0.0, which the
#: `AnalystVerdict` validator reserves for a real neutral read — keeps "low
#: conviction" a genuine (if weak) lean instead of a silent no-lean that
#: would rank identically to a symbol nobody covered.
#: DELETED 2026-09-13 (retired item 31): `NEWS_CONVICTION_MAGNITUDE`,
#: {low 0.33, medium 0.67, high 1.0}. It was a table on `conviction` — the
#: same field this verdict already reports as `conviction` — so
#: `score_verdict`'s two-signal composite counted news's conviction twice, at
#: a spacing nothing stood behind. Magnitude is now `NO_STATED_STRENGTH`
#: (0.0 — this seat has no strength scale of its own and does not borrow
#: Technical's); the conviction it was derived from is unchanged and still
#: carried, and is what reaches the ranking.

#: Same ordinal `_CONVICTION_RANK` idea as `src/nominations.py` and
#: `CONVICTION_SCORE` in `src/verdicts.py` (low < medium < high), kept as a
#: private copy here rather than imported: both of those modules import
#: `src.models`, so importing either back would be circular. This is a
#: single `max()` key, not a quantity computed and compared across files, so
#: it does not trip the one-definition guard's arithmetic-shape matching —
#: see `tests/test_one_definition_guard.py`.
_NEWS_CONVICTION_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Evidence cap for `news_verdict_for_symbol` — see its docstring.
_MAX_NEWS_EVIDENCE_ITEMS = 5


def news_verdict_for_symbol(symbol: str, items: list["StockNewsItem"]) -> "AnalystVerdict":
    """Collapse every `StockNewsItem` the News seat filed for one symbol
    into the one `AnalystVerdict` the Portfolio Manager compares seats by.

    Precondition: `items` is the list filed under one `stock_news` key.
    An empty list is the uncovered marker (`analyze()` fills `[]` for a
    shown symbol the seat omitted, and the model may emit `[]` for an
    incidental mention). Callers that build ranking verdicts skip empty
    lists so UNKNOWN is not collapsed into a fake neutral lean; this
    function itself fails soft (neutral, no lean) rather than raising.

    **direction** — the collapsed sentiment across every item, via
    `src.quantities.collapse_stances` — the SAME reduction
    `PortfolioManagerAgent.build_evidence_registry` already applies to
    `(i.sentiment for i in items)` (see `PortfolioManagerAgent.
    _collapse_stances`, now a thin wrapper over the same function). Reusing
    it rather than inventing a second disagreement rule is deliberate: a
    verdict and a registry stance about the same symbol must never resolve
    a three-way sentiment split differently. `collapse_stances` returns
    "mixed" for any unresolved disagreement (including a directional
    sentiment sitting alongside a "neutral" one) — that is treated as
    neutral here, same as an empty `items` list: an unresolved split is the
    absence of a call, not a third direction `AnalystVerdict` has no room
    for.

    **conviction** — JUDGMENT CALL, flagged for review. `collapse_stances`
    decides the winning DIRECTION but says nothing about conviction, so:
    among the items whose own `sentiment` agrees with the final collapsed
    direction, take the HIGHEST conviction. Rationale: an item that
    disagreed with the eventual call was outvoted and its confidence in the
    losing side is not evidence for how strongly to hold the winning one;
    among the items that agree, the most confident one is the strongest
    stated support the desk actually has for this call. A neutral verdict
    (collapse resolved to neutral, or resolved to "mixed", or `items` was
    empty) has no winning side to draw a conviction from, so it defaults to
    "low" — the weakest assertion the scale offers, since there is
    nothing here to be confident ABOUT.

    **magnitude** — `NO_STATED_STRENGTH` (0.0), directional or not. A news
    item states a sentiment and a conviction, and nothing else about how far
    it leans: a magnitude derived from that conviction would be the same
    signal counted twice in `score_verdict`, and a magnitude borrowed off
    Technical's rungs would be a scale this seat does not have. The seat
    still reaches the ranking through its weighted conviction. See
    `NO_STATED_STRENGTH` for the full reasoning and what was deleted.

    **evidence** — one `VerdictEvidence(label="headline", text=...)` per
    item, `headline` and `impact_summary` joined so the check is visible
    without opening the source article, capped at `_MAX_NEWS_EVIDENCE_ITEMS`
    (JUDGMENT CALL, flagged for review) so a symbol with a long news day
    does not bloat the verdict — earlier items are kept (arrival order,
    unchanged from `NewsIntelligenceReport.stock_news`), on the assumption
    that News files its most decision-relevant item first. Included even
    for a neutral verdict (optional there, but still useful context).

    **invalidation** — JUDGMENT CALL, flagged for review. News items carry
    no stated falsifier the way `TechAnalysisResult.thesis_invalid_if` does,
    but `AnalystVerdict` refuses a directional (non-neutral) verdict with a
    blank one, so something honest has to be constructed.

    The obvious candidate — quote whichever item disagreed with the final
    direction as "the stated case against the call" — turns out to be
    unreachable given `StockNewsItem.sentiment`'s domain (bullish/bearish/
    neutral only): `collapse_stances` only resolves to a directional
    (non-"mixed") result when EVERY surviving sentiment is that exact same
    value (its `len(cleaned) == 1` branch — the positive/negative-SET
    branches below it can never fire for a 3-valued domain where each
    polarity set has exactly one reachable member). So whenever this
    function's `direction` is bullish or bearish, by construction every
    item already agrees with it and there is no opposing item to quote.
    (Proven in `tests/test_news_verdict.py::
    test_a_directional_call_never_has_a_disagreeing_item_to_quote`.)

    So the only honest falsifier available is structural: a later headline
    reporting the opposite sentiment on this symbol. That is a generic,
    templated sentence, not a fabricated specific fact, and it is the same
    sentence for every directional call — flagged so review can judge
    whether that bar is met, or whether "" (like the neutral case) would be
    more honest than a templated non-fact.

    A neutral verdict states no invalidation ("") — a neutral read is the
    absence of a call, so `AnalystVerdict` does not require one and none is
    invented.

    seat — "news", the same key `build_evidence_registry` puts news stances
    under (`put(symbol, "news", ...)`).
    """
    direction = collapse_stances(item.sentiment for item in items) or "neutral"
    if direction not in ("bullish", "bearish", "neutral"):
        # "mixed" (or anything else collapse_stances might someday return
        # that isn't one of the three verdict directions) is an unresolved
        # split — treated as no lean, never guessed at.
        direction = "neutral"

    if direction == "neutral":
        conviction = "low"
        magnitude = 0.0
        invalidation = ""
    else:
        agreeing = [item.conviction for item in items if item.sentiment == direction]
        conviction = max(agreeing, key=lambda c: _NEWS_CONVICTION_RANK.get(c, -1)) if agreeing else "low"
        magnitude = NO_STATED_STRENGTH
        # No opposing item to quote — see the docstring's invalidation
        # section for why that is provably always true here, not merely
        # true of the fixtures this happens to have been tested against.
        opposite = "bearish" if direction == "bullish" else "bullish"
        invalidation = f"a subsequent headline reporting {opposite} sentiment on {symbol}"

    evidence = [
        VerdictEvidence(
            label="headline",
            text=f"{item.headline} — {item.impact_summary}".strip(" —"),
        )
        for item in items[:_MAX_NEWS_EVIDENCE_ITEMS]
    ]

    return AnalystVerdict(
        seat="news",
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=evidence,
        invalidation=invalidation,
    )


class NewsIntelligenceReport(LLMOutputModel):
    macro_narrative: MacroNarrative
    state_changes: list[StateChange] = []
    stock_news: dict[str, list[StockNewsItem]] = {}
    pm_briefing: str
    market_sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: Literal["high", "medium", "low"]
    # Phase 9 (§9.1): a genuine catalyst News wants Technical to look at,
    # even when the symbol never tripped the tech prefilter. Default []
    # so an old persisted/replayed report parses unchanged.
    nominations: list[Nomination] = []
    # PM TEST GATE item 4, second half (2026-09-14). Computed by
    # `NewsAnalystAgent.analyze()` AFTER parsing, exactly like
    # `TechAnalysisResult.computed_levels` — never asked of the model,
    # never invented from it either. Holds every symbol `stock_mentions`
    # (the deterministic, pre-LLM word-boundary match over real wire text —
    # see `NewsDataProvider.tag_symbol_mentions`) proves had real headline
    # content shown to the model, but which had no key at all in the
    # model's `stock_news`. `analyze()` then fills those keys with `[]`
    # so the structured map is complete without inventing a headline;
    # this list still names the omission so data_status stays
    # `symbol_dropped` and downstream seats do not read the empty list
    # as "no news". Default `[]` so an old persisted/replayed report —
    # and every caller that hasn't been updated — parses unchanged.
    # `SkipJsonSchema` is what makes "never asked of the model" true on the
    # wire as well as in this comment: the field used to be rendered into
    # this report's `response_format` schema like any other, so the seat was
    # shown an output slot inviting it to nominate its own coverage gaps,
    # and `analyze()` overwrote whatever it said one line after parsing.
    dropped_news_symbols: Annotated[list[str], SkipJsonSchema()] = []

    def format_dropped_symbols_block(self) -> str:
        """Prompt text naming symbols shown real headlines but omitted from
        the seat's structured answer. Empty string when nothing was lost.

        Shared by PM, Risk, and the position reviewer so incomplete news is
        stated as UNKNOWN in every downstream seat, never left as silence.
        """
        if not self.dropped_news_symbols:
            return ""
        header = (
            "\n\n### News Answer Lost — {n} symbol(s) — NOT an absence of news\n"
            "The news seat had real headline coverage for these symbols "
            "but its structured answer for them did not survive parsing "
            "(a dropped response, not a judgment that there was nothing "
            "to report). Treat coverage for these names as UNKNOWN, "
            "never as clean and never as \"no news\":\n"
        ).format(n=len(self.dropped_news_symbols))
        return header + "\n".join(f"- {sym}" for sym in self.dropped_news_symbols)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values, lower_fields=("market_sentiment", "confidence"),
        )

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_intraday_pnl: float = 0.0
    sector: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class EarningsSegment(LLMOutputModel):
    name: str
    revenue: str
    growth: str = "not disclosed"


class EarningsRevenue(LLMOutputModel):
    total: str
    yoy_growth: str = "not disclosed"
    segments: list[EarningsSegment] = []


class EarningsProfitability(LLMOutputModel):
    gross_margin: str = "not disclosed"
    operating_margin: str = "not disclosed"
    net_income: str = "not disclosed"
    eps: str = "not disclosed"


class EarningsCashFlow(LLMOutputModel):
    operating_cf: str = "not disclosed"
    free_cf: str = "not disclosed"
    capex: str = "not disclosed"


class EarningsBalanceSheet(LLMOutputModel):
    cash_and_equivalents: str = "not disclosed"
    total_debt: str = "not disclosed"
    assessment: str = "not disclosed"


class EarningsStrategicDirection(LLMOutputModel):
    key_initiatives: list[str] = []
    capital_allocation: str = "not disclosed"
    competitive_positioning: str = "not disclosed"


class EarningsRiskFlags(LLMOutputModel):
    strategic_risks: list[str] = []
    operational_risks: list[str] = []


class EarningsReasoningChain(LLMOutputModel):
    """5-step CoT for fundamental analysis — why sentiment is what it is.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.
    """
    fundamental_quality: str = Field(min_length=1)       # revenue, margin, cash flow trajectory
    growth_trajectory: str = Field(min_length=1)         # YoY / QoQ direction, momentum, inflection
    strategic_risks: str = Field(min_length=1)           # biggest strategic bets and their execution risk
    management_execution: str = Field(min_length=1)      # is management doing what they said? any pivots?
    # NOT "is the market pricing this fairly" — the agent is given filing text
    # and nothing else (no share price, no market cap, no multiple), so it
    # cannot answer that and inventing an answer is what it used to do. Reads
    # "how conditional is the story": what must keep holding for the disclosed
    # trajectory to justify any premium. See config/prompts/earnings_analyst.md.
    valuation_context: str = Field(min_length=1)


class EarningsInvestmentImplications(LLMOutputModel):
    sentiment: Literal["bullish", "bearish", "neutral"]
    conviction: Literal["high", "medium", "low"]
    reasoning_chain: EarningsReasoningChain
    key_thesis: str
    bull_case: str = "not disclosed"
    bear_case: str = "not disclosed"

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values, lower_fields=("sentiment", "conviction"),
        )


class EarningsAnalysis(LLMOutputModel):
    symbol: str
    form_type: Literal["10-Q", "10-K"]
    filing_date: str
    revenue: EarningsRevenue
    profitability: EarningsProfitability
    cash_flow: EarningsCashFlow
    balance_sheet: EarningsBalanceSheet
    management_highlights: list[str] = []
    guidance: str
    strategic_direction: EarningsStrategicDirection = EarningsStrategicDirection()
    risk_flags: EarningsRiskFlags | list[str] = EarningsRiskFlags()
    strategy_consistency: str = "No prior filing available for comparison"
    investment_implications: EarningsInvestmentImplications
    data_quality: str
    # Phase 9 (§9.1): a filing that materially changes the picture — most
    # often for the symbol this very filing is about, since a blowout beat
    # on a name Technical never rated is exactly the gap Phase 9 closes.
    # Default [] so an analysis saved to disk before this field existed
    # still loads unchanged (EarningsAnalystAgent._load_analysis).
    #
    # This is the per-FILING output model, not a per-run container — no
    # per-run earnings container exists in this codebase (`earnings_results`
    # on RunContext is a plain `list[dict]` the pipeline assembles, not a
    # Pydantic model). A session that analyzes multiple new filings makes
    # one LLM call per filing, so nominations are aggregated across every
    # filing's analysis this run (`_collect_seat_nominations`), the same
    # way `earnings_results` itself already aggregates per-filing output.
    nominations: list[Nomination] = []

    def to_verdict(self) -> "AnalystVerdict":
        """This filing's read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion: every field is read off
        `investment_implications`, which the earnings analyst already fills
        and already validates (`EarningsInvestmentImplications`) — no new
        prompting.

        direction  — `investment_implications.sentiment`, verbatim (already
                     bullish/bearish/neutral, the exact vocabulary the shared
                     shape uses).
        conviction — `investment_implications.conviction`, verbatim.
        magnitude  — UNLIKE Technical (which has two directional rungs a
                     side — buy/strong_buy — to encode as 0.5/1.0), earnings
                     sentiment is a single bullish/bearish rung with no
                     numeric or ordinal strength field anywhere on this
                     model or `EarningsInvestmentImplications`: `risk_flags`
                     is unstructured lists of free-text risks, and
                     `data_quality` is free prose, not a graded scale.
                     Inventing a gradient from either would be a fake
                     precision this seat cannot back. So every call carries
                     `NO_STATED_STRENGTH` (0.0) — no distance claimed, rather
                     than a distance borrowed off Technical's scale. The seat
                     still reaches the ranking through its weighted
                     conviction.

                     REVIEWED 2026-09-13 (retired item 31) and KEPT
                     unchanged — this was the only one of the four new seats
                     that did not invent a gradient, and the other three were
                     brought to this shape rather than the reverse. The bare
                     0.5 became the shared constant so there is one number.
        evidence   — `key_thesis` (the seat's own summary of its call) plus
                     the five reasoning-chain steps, labelled, plus
                     `data_quality` when the analyst said anything past the
                     bare default.
        invalidation — the case the analyst built AGAINST its own call:
                     `bear_case` for a bullish read, `bull_case` for a
                     bearish one. Both fields default to the literal string
                     "not disclosed" when the analyst didn't fill them in
                     (see the field definitions above) — that placeholder is
                     not a real falsifier, so it is treated as blank rather
                     than passed through as if it were content. UNLIKE
                     Technical, there is no numeric stop-price to fall back
                     to here, so a directional call whose falsifier is left
                     undisclosed ends up with a blank `invalidation` and
                     `AnalystVerdict`'s own validator refuses to construct
                     it — this seat cannot manufacture a falsifier out of
                     nothing, and an error at construction is more honest
                     than inventing one.
        """
        impl = self.investment_implications
        direction = impl.sentiment
        # 0.0 either way — see `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH

        evidence: list[VerdictEvidence] = []
        if impl.key_thesis.strip():
            evidence.append(VerdictEvidence(label="key_thesis", text=impl.key_thesis.strip()))
        chain = impl.reasoning_chain
        for label in (
            "fundamental_quality", "growth_trajectory", "strategic_risks",
            "management_execution", "valuation_context",
        ):
            text = getattr(chain, label, "") or ""
            if text.strip():
                evidence.append(VerdictEvidence(label=label, text=text.strip()))
        data_quality = (self.data_quality or "").strip()
        if data_quality and data_quality.lower() != "not disclosed":
            evidence.append(VerdictEvidence(label="data_quality", text=data_quality))

        if direction == "bullish":
            falsifier = impl.bear_case
        elif direction == "bearish":
            falsifier = impl.bull_case
        else:
            falsifier = ""
        falsifier = (falsifier or "").strip()
        invalidation = "" if falsifier.lower() == "not disclosed" else falsifier

        return AnalystVerdict(
            seat="earnings",
            symbol=self.symbol,
            direction=direction,
            magnitude=magnitude,
            conviction=impl.conviction,
            evidence=evidence,
            invalidation=invalidation,
        )

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("filing_date")
    @classmethod
    def validate_filing_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("guidance", "data_quality")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("field cannot be empty")
        return text

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)


class PositionAction(LLMOutputModel):
    # Stage 3 (shorts): COVER is the short-side twin of SELL/REDUCE for the
    # intraday reviewer — closes/trims a held SHORT, never opens one. The
    # reviewer's prompt (config/prompts/position_reviewer.md) asks for it,
    # and `TradingPipeline._midday_execute_llm_actions` executes it: same
    # named-trigger gate, exit-guard veto, noise band, same-day-trim
    # discipline and AI Risk routing a SELL/REDUCE gets, always as a full
    # close (this schema carries no allocation fraction for it).
    action: Literal["SELL", "REDUCE", "TRAIL_STOP", "COVER", "HOLD"]
    symbol: str
    reason: str
    new_stop_price: float | None = None  # required when action == TRAIL_STOP

    # 2026-09-18. `reason` alone was the whole of an exit's justification,
    # and the whole of its CHECK was a substring match over this prose
    # (`pipeline._reason_cites_hard_trigger`). On 2026-09-16 both real
    # exits carried the reason "adverse news" — two words, entire — and
    # passed every gate, because the phrase is on the list and no checker
    # could tell which claim was being made. These two fields move the
    # claim out of the sentence and into the schema so it becomes
    # decidable; see `src/risk/exit_trigger.py` for the full write-up.
    #
    # Both are OPTIONAL and default to "not stated". That is deliberate,
    # not laxity: an action carrying only prose is mechanically healed
    # from the same phrase vocabulary as before, so nothing that used to
    # execute stops executing on paperwork. `ExitTrigger
    # .CANNOT_SUBSTANTIATE` is a first-class value precisely so the seat
    # is never pushed into naming a trigger it cannot support — recording
    # it is a correct answer that triggers a re-ask, not a refusal.
    exit_trigger: ExitTrigger | None = None
    trigger_evidence: str = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # Action is UPPERCASE per Literal — fold LLM drift like "sell".
        values = _normalize_enum_case_fields(values, upper_fields=("action",))
        # `exit_trigger` is lower_snake per ExitTrigger; fold "Adverse News"
        # and "ADVERSE-NEWS". An UNRECOGNISED value becomes None rather
        # than a ValidationError: raising would drop the whole action (see
        # `PositionReviewerAgent._drop_invalid_actions`) and losing an exit
        # over a misspelled enum is the failure direction this path is
        # explicitly not willing to take. None means "no trigger named",
        # which is then healed from the prose.
        if isinstance(values, dict) and "exit_trigger" in values:
            raw = values.get("exit_trigger")
            coerced = normalize_trigger(raw)
            if coerced is None and raw not in (None, ""):
                logger.warning(
                    "PositionAction: unrecognised exit_trigger %r on %s — "
                    "read as 'no trigger named' and healed from the reason",
                    raw, values.get("symbol"),
                )
            values["exit_trigger"] = coerced
        return values

    @model_validator(mode="after")
    def _trail_stop_requires_new_price(self):
        if self.action == "TRAIL_STOP" and (self.new_stop_price is None or self.new_stop_price <= 0):
            raise ValueError("TRAIL_STOP requires new_stop_price > 0")
        return self


class TargetRevisionFlag(LLMOutputModel):
    """A seat's claim that a held position's take-profit was measured against
    structure that no longer exists. EVIDENCE ONLY — there is no price field.

    This schema carries no target price ON PURPOSE. A revision is a
    RE-DERIVATION: `src.risk.target_revision.assess_target_revision` decides
    whether a structural event legitimises re-asking, and
    `src.data.levels.derive_structural_target` supplies the number from
    today's bars. The seat supplies the observation; the code supplies the
    price. Accepting a typed target here would put a model-guessed number on
    the DENOMINATOR of `thesis_progress_pct` and `pace` — i.e. docs/WORK.md
    item 80 (stop provenance) reappearing on the field that feeds the exit
    guard — so the field simply does not exist to be filled in.

    Raising a flag is not a revision. The overwhelmingly common outcome is
    `REFUSAL_NO_STRUCTURAL_EVENT`, recorded per symbol: a view that a name
    has further to run is not a structural event.
    """

    symbol: str
    evidence: str = Field(min_length=1)
    """WHAT was observed about the structure, not what the seat expects.
    "gapped through and closed above the 214 resistance on earnings" is
    evidence; "I think there is more upside here" is not and will be
    refused."""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class PositionReasoningChain(LLMOutputModel):
    """Six-step chain the position reviewer must fill before emitting actions.

    Parallel depth to morning PM's 7-step reasoning_chain — prevents
    intraday-price knee-jerk selling and forces memory-aware, thesis-driven
    decisions. Each field is required; empty strings will fail validation
    so the agent can't skip a step by sending "".
    """
    macro_continuity_check: str = Field(min_length=1)
    """Regime + outlook today vs morning vs this week. Stable ⇒ HOLD bias."""

    thesis_progress_check: str = Field(min_length=1)
    """Per-position thesis_progress_pct / pace / distance-to-stop|target.
    Distinguishes 'fast mover' / 'on pace' / 'stalled' / 'broken'."""

    thesis_integrity_check: str = Field(min_length=1)
    """Every SELL/REDUCE must cite a specific named trigger — thesis_invalid_if
    condition, HIGH-conviction state_change reversal, or bearish earnings
    analysis. Intraday price alone is NOT a trigger, and "correlation breach"
    stopped being one 2026-09-13 (nothing can verify it)."""

    winners_discipline_check: str = Field(min_length=1)
    """For positions with profit > 10%: is momentum fading, is it parabolic,
    has target been exceeded? If no, default is HOLD regardless of size —
    good stocks are meant to be held."""

    session_disposition_check: str = Field(min_length=1)
    """Session-aware framing: 'midday' = afternoon patience, TRAIL_STOP over
    SELL; 'close' = act-if-triggered-not-act-because-time, 17.5h no control,
    act only on clear thesis signals never on clock-driven fear."""

    execution_rationale: str = Field(min_length=1)
    """For each SELL/REDUCE action, a 'lock now' vs 'hold outcome' comparison.
    HOLD needs no comparison. TRAIL_STOP names the upside protected vs given up."""


class PositionReview(LLMOutputModel):
    reasoning_chain: PositionReasoningChain
    actions: list[PositionAction] = []
    overall_assessment: str = Field(min_length=1)
    risk_level: Literal["low", "moderate", "elevated", "high"]
    target_revision_flags: list[TargetRevisionFlag] = []
    """Positions whose take-profit was measured against structure the seat
    observes is gone. Evidence only — see `TargetRevisionFlag`. Each flag is
    adjudicated by `src.risk.target_revision` and recorded per symbol
    whichever way it goes; a flag is never an instruction and never an exit."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(values, lower_fields=("risk_level",))


class EveningReasoningChain(LLMOutputModel):
    """Seven-step chain evening analyst must fill before emitting the report.

    Depth parallel to PM's 7-step and position_reviewer's 6-step chains.
    Empty strings fail validation — the agent cannot skip a step. Gives
    evening the same thought-depth structure as other LLM agents so its
    decisions are auditable, not just narrative.

    Design note (2026-04 upgrade): the previous 6-step chain was
    structurally anchored on DAILY cycles (yesterday's outlook, today's
    tape, tomorrow's preparation). For a medium-long-term investor, the
    most important question — "how is each held thesis playing out over
    the past 6-8 weeks?" — wasn't being asked anywhere. `thesis_health_
    review` is that missing step, and it sits between the retrospective
    (what happened) and the decision-quality review (how did we react).
    """
    performance_attribution: str = Field(min_length=1)
    """What drove today's P&L? Which positions contributed + / −, which macro /
    news factors explain the moves. Concrete, not vague."""

    outlook_retrospection: str = Field(min_length=1)
    """Honest grade of yesterday's tomorrow_outlook vs today's actual. If
    yesterday said bullish and today ripped down, say so. Calibration > saving
    face. Cross-reference specific predictions to specific outcomes."""

    thesis_health_review: str = Field(min_length=1)
    """For each held position: given 6-8 weeks of fundamentals evolution
    (earnings trajectory, macro sector stance, news flow, tech rating
    history), is the ORIGINAL entry thesis strengthening, still intact,
    weakening, or broken? This is the step that makes the agent a value
    investor not a swing trader. For holdings where the thesis is
    broken — flag them for SELL consideration tomorrow even if price
    hasn't yet moved. For holdings where the thesis is strengthening
    but price hasn't caught up — flag them as add-more candidates.
    Price noise is not thesis noise; conflating them is the main way
    medium-long-term strategies go wrong."""

    decision_quality_review: str = Field(min_length=1)
    """BUY / SELL / HOLD decisions today + the last few days. Pattern check:
    are you selling winners too early? Buying near tops? Hedging at the wrong
    time? Name the pattern if one exists."""

    calibration_meta: str = Field(min_length=1)
    """Zoom out on your recent bias / conviction track record (surfaced in the
    prompt). Are you systematically too bullish? Does HIGH conviction actually
    outperform LOW? This is the meta-loop — learning from your own accuracy
    not just yesterday's single call."""

    market_regime_read: str = Field(min_length=1)
    """Where is the market now, where's it going, what's the key evidence from
    today's tape + news. This is the foundation the tomorrow_bias rests on."""

    tomorrow_preparation: str = Field(min_length=1)
    """Key events tomorrow (earnings, econ data, Fed), levels to watch, how
    today's action shapes tomorrow's posture. What PM needs to know at 09:30."""


# Thesis-trajectory classifier for trade grading — the 2nd dimension that
# separates "swing trader" feedback from "value investor" feedback. A buy
# can be down 10% with the thesis still intact (noise); a buy can be up 10%
# with the thesis broken (momentum, not value). Grade must weigh BOTH
# price AND thesis; this enum carries the latter.
ThesisTrajectory = Literal[
    "strengthening",   # new data since entry reinforces the thesis
    "intact",          # no new negative information, reasons still valid
    "weakening",       # some contrary data but thesis isn't yet broken
    "broken",          # thesis invalidated by hard data (earnings miss,
                       # guidance cut, regulatory action, etc.)
]


class SellGrade(LLMOutputModel):
    """Structured grade of a single recent SELL — what evening judged right or
    wrong. PM / position reviewer can read aggregate counts to feed back into
    their SELL discretion.

    Grading is dual-axis: `grade` aggregates `price_outcome` (what the tape
    did since we sold) and `thesis_trajectory_at_sell` (whether we sold
    with thesis-justification or on nerves / noise). A defensible SELL
    is one where we exited a weakening/broken thesis, even if price
    subsequently bounced — we kept discipline. A `wrong` SELL is one
    where we exited an intact/strengthening thesis AND price ran.
    """
    symbol: str
    sell_date: str   # "YYYY-MM-DD"
    sell_price: float
    current_price: float
    pct_move_since_sell: float
    grade: Literal["correct", "premature", "wrong"]
    reason: str = Field(min_length=1)
    # 2nd dimension added 2026-04 (value-lens upgrade). Optional so
    # pre-upgrade rows still parse, but the evening prompt now requires
    # LLM to fill it for every new grade it emits.
    thesis_trajectory_at_sell: ThesisTrajectory | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values, lower_fields=("grade", "thesis_trajectory_at_sell"),
        )


# Root-cause taxonomy for losing BUYs. Used by evening_analyst when a
# buy_grade is "wrong" so the quarterly meta-reflector can aggregate
# patterns ("3 of our last 10 wrongs were greed_top_chasing → tech_analyst
# prompt needs an ATR-upper-band guard"). Ordering below mirrors priority
# for tie-breaking when multiple apply: self-inflicted root causes first,
# systemic / unavoidable ones last (don't let the LLM default to the easy
# "tail_event" out).
BuyLossRootCause = Literal[
    "greed_top_chasing",      # entered near top, momentum chased, no margin of safety
    "macro_warning_ignored",  # macro/news signals warned, we ignored (must cite evidence)
    "herd_buying",            # bought because news was loud, no independent thesis
    "averaged_down",          # added to loser past stop discipline
    "thesis_broken_held",     # thesis invalidated by data but we didn't sell
    "concentration_blow",     # single sector/theme overweight turned
    "timing_mistake",         # thesis correct, timing off — least-blameworthy class
    "systemic_drawdown",      # broad market fell; we fell with it (not alpha destruction)
    "tail_event",             # real black-swan; rare; LLM should resist defaulting here
]


class BuyGrade(LLMOutputModel):
    """Structured grade of a recent BUY — did the entry play out?
    Mirrors SellGrade so the feedback loop is symmetric.

    Like SellGrade, grading is dual-axis. `grade` aggregates price
    action AND `thesis_trajectory` (how the underlying fundamentals /
    theme have evolved since entry). A buy can be down 8% with thesis
    strengthening — that's NOT wrong, that's value entry being tested
    by noise. A buy can be up 10% with thesis broken — that's NOT
    correct, that's momentum masking a real failure."""
    symbol: str
    buy_date: str
    buy_price: float
    current_price: float
    pct_move_since_buy: float
    grade: Literal["correct", "premature", "wrong"]
    reason: str = Field(min_length=1)
    # 2nd grading dimension. Optional for back-compat; prompt requires it
    # on all new grades.
    thesis_trajectory: ThesisTrajectory | None = None
    # Loss-autopsy fields: required only when grade == "wrong". Evening analyst
    # must classify WHY a losing BUY lost so quarterly meta-reflection can
    # aggregate patterns and propose targeted prompt edits. Optional on
    # correct/premature so existing fixtures stay valid.
    loss_root_cause: BuyLossRootCause | None = None
    # SPY return over the same window as pct_move_since_buy. Python-injected
    # by the pipeline before passing to the LLM. Positive number when we
    # under-performed the market (alpha destruction); ~0 or negative when
    # the whole market fell (systemic). Lets the LLM distinguish greed_top_chasing
    # from systemic_drawdown without pattern-matching prose.
    market_relative_move_pct: float | None = None
    # Required when loss_root_cause == "macro_warning_ignored": the specific
    # warning that was visible at entry and dismissed. Format expected:
    # "<agent> <date> <conviction>: <headline>" — evidence, not vibes.
    missed_warning_ref: str | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("grade", "thesis_trajectory", "loss_root_cause"),
        )

    @model_validator(mode="after")
    def _loss_fields_required(self) -> "BuyGrade":
        if self.grade == "wrong" and self.loss_root_cause is None:
            raise ValueError(
                "BuyGrade with grade='wrong' requires loss_root_cause so the "
                "quarterly meta-reflector can aggregate patterns"
            )
        # A 'wrong' grade also needs thesis_trajectory so position_reviewer
        # can distinguish "bought expensive" (intact thesis, price-only
        # mistake — re-entry candidate when price comes back) from
        # "fundamentals broke" (broken thesis — stay out). Without both
        # fields together, the loss-autopsy loop loses half its information
        # and the next-day prompt can't apply the value-investor lens.
        # Optional on correct/premature for back-compat.
        if self.grade == "wrong" and self.thesis_trajectory is None:
            raise ValueError(
                "BuyGrade with grade='wrong' requires thesis_trajectory so "
                "position_reviewer can distinguish a value re-entry candidate "
                "(intact thesis) from a stay-out signal (broken thesis)"
            )
        if (self.loss_root_cause == "macro_warning_ignored"
                and not (self.missed_warning_ref or "").strip()):
            raise ValueError(
                "loss_root_cause='macro_warning_ignored' requires missed_warning_ref "
                "citing the specific signal that was ignored (agent + date + headline)"
            )
        return self


class MissedOpportunitySnapshot(BaseModel):
    """Python-computed facts for one notable mover — INPUT to the evening LLM,
    not its output. The LLM reads a list of these and writes one
    MissedOpportunity per interesting row.

    Carries enough signal-state context (prior TA rating, recent news
    headline, earnings signal, macro sector stance) that the LLM's miss
    classification has to be grounded in observable prior evidence rather
    than price retro-rationalization.

    For symbols sourced from Alpaca's top-mover screener (not in our
    trading universe), the quality fields (avg_dollar_volume_20d_m,
    volume_confirmation_ratio, single_day_concentration_pct) are the
    main filter for "worth considering adding to universe" vs "low-
    volume squeeze we should ignore". A medium-long-term investor
    doesn't chase thin moves.
    """
    symbol: str
    move_pct: float
    window_days: int
    held_during_window: bool
    had_ta_signal: bool
    had_news_signal: bool
    had_earnings_signal: bool
    source: Literal["universe", "top_mover", "both"]
    # Optional evidence the LLM should cite in its `lesson`.
    last_ta_rating: str | None = None          # e.g. "hold" / "buy"
    last_ta_date: str | None = None            # ISO YYYY-MM-DD
    last_news_headline: str | None = None      # trimmed ≤ 140 chars upstream
    # Theme fingerprint the LLM can adopt in MissedOpportunity.theme_if_any.
    # Populated from recent news state_changes / earnings IIC tags.
    theme_tags: list[str] = []
    # Latest earnings-analyst take if this symbol reported in last ~90d.
    # Trimmed to ≤ 140 chars upstream. Lets the LLM flag
    # "fundamentals_mispricing" only when there's real fundamental backing.
    recent_earnings_signal: str | None = None
    # Macro's sector_guidance direction for this symbol's sector, recent call.
    # "unknown" = macro never covered the sector (itself a signal — blindspot).
    macro_sector_tailwind: Literal["bullish", "neutral", "bearish", "unknown"] = "unknown"

    # Quality metrics — primary lens for whether a top-mover deserves
    # watchlist consideration. Filled by Python from bar data; None when
    # insufficient bars to compute reliably.
    avg_dollar_volume_20d_m: float | None = None
    """20-day average daily dollar volume in MILLIONS of USD. Low numbers
    (< ~5M) indicate thin liquidity — easy to squeeze, dangerous for a
    medium-long-term position. Used to pre-filter very illiquid movers
    upstream; the LLM also sees it to reason about "real institutional
    interest vs low-volume drift"."""
    volume_confirmation_ratio: float | None = None
    """Today's dollar volume / 20-day avg. > ~1.5 indicates buyers
    showed up in size (real interest). < 1.0 = move happened on
    normal-or-less flow; unlikely to sustain."""
    single_day_concentration_pct: float | None = None
    """Percent of the window's total return that came from the BIGGEST
    single day. 0-100. > 70 = gap-up day (event / squeeze); < 50 =
    distributed move (trend). For a medium-long-term investor, a
    distributed trend is far more interesting than a single gap."""

    # Valuation context (2026-04 upgrade — value-lens). Yahoo data via
    # MarketDataProvider.get_valuation_metrics. None when ETF / not
    # available. The LLM should not chase stretched-PE symbols even if
    # they pass the quality bars.
    trailing_pe: float | None = None
    forward_pe: float | None = None
    ps_ratio: float | None = None
    valuation_signal: Literal["cheap", "fair", "stretched", "no_data"] = "no_data"
    """Rough forward-PE-based classifier filled by Python upstream.
    < 12 → cheap, 12-25 → fair, >= 25 → stretched, None → no_data.
    Thresholds are deliberately crude — the LLM reads raw PE numbers
    too and makes sector-adjusted judgments. `valuation_signal` is
    just a fast first cut that prevents obvious hype chasing."""

    # Bidirectional opportunity framing. Default False; set True by
    # digest when move_pct < -8% AND there is intact fundamental/theme
    # signal — classic "price panicked, thesis didn't" value dip.
    value_entry_candidate: bool = False

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)


class MissedOpportunity(LLMOutputModel):
    """Evening-analyst OUTPUT for one snapshot: classified miss + lesson +
    (for non-universe symbols) watchlist-addition recommendation.

    `miss_category` frames the miss through the three lenses the user cares
    about: catching trends, not missing themes, spotting fundamental
    mispricing. `noise_rally` and `risk_disciplined` are escape hatches so
    the LLM isn't forced to label every price move as a miss — but the
    prompt has to push back when they're overused.

    For symbols sourced from the top-mover screener (not in the trading
    universe), `universe_addition_recommendation` is the high-bar answer
    to "should we add this to the 77-symbol universe we carefully curated?"
    Default is "no" — the universe is deliberately small; thin or
    one-day-gap moves should not expand it. "add" only when volume,
    sustain, theme, and fundamentals all point in the right direction.
    """
    symbol: str
    move_pct: float
    miss_category: Literal[
        "trend_timing_miss",        # trend visible, entry late or absent
        "theme_blindspot",          # entire theme/sector uncovered by our agents
        "fundamentals_mispricing",  # hard earnings numbers, price not yet reacting
        "value_entry_missed",       # stock DOWN >=8% with thesis intact, we
                                    # didn't add — classic value dip missed
        "noise_rally",              # no signal, legitimate HOLD — not a real miss
        "risk_disciplined",         # RM / hard-rule blocked, accepted — not a real miss
    ]
    # Free-form theme label the LLM picks (e.g. "AI-capex", "nuclear/power",
    # "rare-earth", "reshoring"). Required for trend / theme / mispricing /
    # value categories so the quarterly digest can aggregate. None when
    # miss_category is noise_rally / risk_disciplined.
    theme_if_any: str | None = None
    # Theme duration classifier — "looks excellent" is not enough for the
    # user's 77-symbol universe; we want to distinguish a 2-month hype
    # cycle from a decade-long secular trend. Required when theme_if_any
    # is set; optional otherwise.
    theme_durability: Literal[
        "multi_year_secular",   # decade+ structural trend (AI capex, energy
                                # transition, aging demographics)
        "1_3_year_cycle",       # cyclical opportunity (rate cuts, capex
                                # cycle, inventory correction)
        "months_fad",           # short-lived hype (meme, single-event pop,
                                # narrative rotation)
        "unknown",              # not enough information to classify
    ] = "unknown"
    lesson: str = Field(min_length=1, max_length=400)
    # Watchlist-addition recommendation (only meaningful for top-mover sources;
    # default "no" for universe symbols since they're already tracked).
    # High bar: "add" requires documented, multi-factor justification —
    # volume confirmation + multi-day sustain + theme/fundamental anchor +
    # reasonable valuation.
    universe_addition_recommendation: Literal["add", "watch", "no"] = "no"
    universe_addition_reason: str = Field(default="", max_length=400)
    """1-2 sentences citing the QUALITY metrics (volume, sustain, theme,
    fundamentals, valuation) that justify a non-'no' recommendation.
    Required when recommendation is "add" or "watch"; must stay empty
    when "no" so the reason field doesn't drift into wishful thinking."""

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _normalize_symbol(v)

    @model_validator(mode="after")
    def _theme_required_for_real_misses(self) -> "MissedOpportunity":
        real_miss_categories = {
            "trend_timing_miss", "theme_blindspot",
            "fundamentals_mispricing", "value_entry_missed",
        }
        if self.miss_category in real_miss_categories:
            if not (self.theme_if_any or "").strip():
                raise ValueError(
                    f"MissedOpportunity miss_category='{self.miss_category}' "
                    f"requires theme_if_any so quarterly aggregation can group by theme"
                )
        return self

    # audit round 2 #31: the former `_theme_durability_required_when_themed`
    # validator (raise when theme_if_any set and theme_durability is None)
    # was provably unreachable dead code: theme_durability is a non-Optional
    # Literal with default "unknown", so an omitted field silently becomes
    # "unknown" and an explicit null used to fail FIELD-level Literal
    # validation before any mode="after" model validator could run. Deleted
    # rather than "wired" — the docstring above explicitly permits "unknown"
    # as an allowed (if rare) value, so raising on it would contradict the
    # schema contract and get whole entries dropped by the evening pre-filter.
    #
    # UPDATED 2026-09-02: that last sentence turned out to describe what was
    # ALREADY happening. The field-level rejection this note treats as
    # incidental was dropping 25 of every 50 entries in production, for
    # exactly the reason the note gives as the argument against raising.
    # `LLMOutputModel._explicit_null_means_absent` now reads the null as an
    # absent key, so a nulled durability becomes "unknown" and the entry
    # survives. The validator stays deleted; the test that pinned the old
    # mechanism was inverted rather than removed
    # (tests/test_agents_audit_round2.py::
    #  test_idx31_explicit_null_durability_is_now_unknown_not_a_dropped_entry).

    @model_validator(mode="after")
    def _addition_recommendation_consistency(self) -> "MissedOpportunity":
        # "add" / "watch" require a concrete reason; "no" forbids one so
        # the field doesn't become a dumping ground for weak opinions.
        if self.universe_addition_recommendation in ("add", "watch"):
            if not (self.universe_addition_reason or "").strip():
                raise ValueError(
                    f"universe_addition_recommendation="
                    f"'{self.universe_addition_recommendation}' requires "
                    f"universe_addition_reason citing volume, sustain, or "
                    f"theme quality — bar is high, evidence must be concrete"
                )
        return self


class EveningReport(LLMOutputModel):
    # Three Literal enums (risk_rating + tomorrow_bias + tomorrow_conviction)
    # are case-folded BEFORE Pydantic validates — LLMs occasionally drift to
    # uppercase variants like "MODERATE" which would otherwise reject the
    # whole evening output. See _normalize_enum_case_fields docstring.
    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=(
                "risk_rating", "tomorrow_bias", "tomorrow_conviction",
            ),
        )

    reasoning_chain: EveningReasoningChain
    daily_summary: str = Field(min_length=1)
    lessons: str = Field(min_length=1)
    tomorrow_outlook: str = Field(min_length=1)  # prose narrative for PM context
    risk_rating: Literal["low", "moderate", "elevated", "high"]
    suggested_actions: list[str] = []
    # Outlook-vs-reality retrospection — was yesterday's tomorrow_outlook right?
    previous_outlook_assessment: str = ""
    # Structured version of tomorrow_outlook so PM can act on it deterministically
    # instead of re-parsing prose. PM tilts base sizing ±20% on the bias/conviction
    # pair at morning open.
    tomorrow_bias: Literal["bullish", "neutral", "bearish"] = "neutral"
    tomorrow_conviction: Literal["high", "medium", "low"] = "medium"
    tomorrow_key_risks: list[str] = []
    # SELL discipline feedback loop — prose summary retained for narrative
    # continuity + backward compat.
    sell_decisions_assessment: str = ""
    # Structured per-trade grades. PM / position reviewer can compute aggregate
    # stats ("last 14d: correct 5 / premature 3 / wrong 1") from these without
    # parsing prose. Empty list = no grades this session (no recent trades or
    # LLM skipped). Both lists are filled by the LLM from the `recent_*`
    # tables surfaced in the prompt.
    sell_grades: list[SellGrade] = []
    buy_grades: list[BuyGrade] = []
    # What we missed today — up to ~15 entries, one per notable mover not
    # owned during the window. Empty when no universe/top-mover symbols
    # crossed the move_threshold_pct. Feeds next-day PM's L3d memory and
    # the quarterly meta-reflector's theme_coverage_report.
    missed_opportunities: list[MissedOpportunity] = []

    # Medium-term thesis catalysts (2026-04 value-lens upgrade) —
    # complements `tomorrow_key_risks` with a this-week / next-week view
    # on events that would confirm or break held theses. Examples:
    # "NVDA reports Q1 earnings Thu after close", "FOMC minutes next Wed
    # — rate-sensitive sleeves at risk", "MU guidance cut window if
    # memory ASP data disappoints". 0-6 entries, each specific.
    this_week_thesis_catalysts: list[str] = []

    # Structured lesson categories (2026-04 value-lens upgrade). The
    # prose `lessons` field is retained for back-compat / continuity,
    # but downstream agents prefer these three lists when they exist:
    # - thesis_updates: specific held-position thesis changes ("NVDA
    #   thesis strengthening — data-center capex Q1 guide +18%").
    # - selection_rules: new stock-selection insights ("on theme plays,
    #   require ≥2 confirming fundamental prints before sizing >5%").
    # - discipline_notes: behavioral / process reminders ("stop cutting
    #   GOOGL on single-day -2% wobbles; 5 of 7 recent sells premature").
    # All optional; LLM may fill one, two, or all three depending on
    # the day.
    thesis_updates: list[str] = []
    selection_rules: list[str] = []
    discipline_notes: list[str] = []


class AgentLog(BaseModel):
    agent_name: str
    run_id: str
    timestamp: datetime
    input_summary: str
    output_summary: str
    full_response: str
    model: str
    tokens_used: int


# ---------------------------------------------------------------------------
# Quarterly Meta-Reflection schema (PR3+ — strategic self-audit)
# ---------------------------------------------------------------------------

# Agents that meta-reflection is ALLOWED to propose prompt edits to. The two
# excluded agents (risk_manager, position_reviewer) encode hard discipline
# (R/R ≥ 1.5, SELL triggers, cash-only); letting auto-evolution append
# "learnings" there risks diluting invariants. Explicit allow-list is safer
# than a deny-list.
MetaReflectionAgentName = Literal[
    "tech_analyst",
    "news_analyst",
    "macro_analyst",
    "earnings_analyst",
    "portfolio_manager",
    "evening_analyst",
]


class MetaReasoningChain(LLMOutputModel):
    """7-step chain the meta-reflector must fill before emitting the report.

    Parallel depth to morning PM's 7-step chain and position reviewer's
    6-step chain — empty strings fail validation so the LLM can't skip a
    step.

    **Ordering matters**: the LLM runs these in-order to avoid the
    trap of proposing prompt edits without first understanding (a) its
    own self-portrait across multiple axes, (b) where the self-portrait
    falls short of the ideal, (c) what the target agent's prompt ALREADY
    contains. Facts-first, synthesis-next, existing-design-audit,
    proposal-last.

    Design notes for anyone editing this chain:
      - Steps 1-3 are FACTS. They each cite numbers from a specific
        digest section. No interpretation allowed.
      - Step 4 is SYNTHESIS. It's the first step that interprets the
        facts, producing a multi-axis self-portrait. Replaces the old
        single-axis `style_bias_identification` + absorbs the old
        `agent_hit_rate_audit` (which was just another axis of self-
        portrait anyway).
      - Step 5 is DIAGNOSIS. It names 2-3 top leverage gaps between
        the self-portrait and the idealized trader profile the user
        wants the system to converge toward.
      - Step 6 is PROMPT AUDIT. For each gap named in step 5, the LLM
        consults `agent_prompts_snapshot` to understand what's already
        in the target agent's prompt — preventing duplicate / redundant
        / conflicting edits.
      - Step 7 is PROPOSAL. Grounded in both the gaps (step 5) AND the
        existing prompt state (step 6).

    The old `missed_theme_diagnosis` step was folded into
    `portrait_gap_diagnosis` — theme coverage IS one of the gap axes.
    """
    performance_vs_benchmark: str = Field(min_length=1)
    """Step 1/FACT. Where did this quarter's return land vs SPY? Alpha
    positive or negative? Drawdown profile? Be specific about numbers
    from period_performance — no "we did ok this quarter" hand-waving."""

    secular_theme_audit: str = Field(min_length=1)
    """Step 2/FACT. Enumerate this quarter's real themes (AI capex,
    nuclear/power, rare earth, reshoring, etc.). For each: did we
    participate? At what entry position relative to the breakout? For
    how long? Name themes_caught_early, themes_caught_late,
    themes_missed_entirely — mirror the structured output fields."""

    loss_autopsy_audit: str = Field(min_length=1)
    """Step 3/FACT. Enumerate the top 3-5 loss causes from
    loss_patterns.by_cause. For each: count, alpha_destruction_pct,
    which agent owns it. This feeds `loss_pattern_report`."""

    self_portrait_synthesis: str = Field(min_length=1)
    """Step 4/SYNTHESIS. **Multi-axis self-portrait**, not a single-
    line label. Synthesize facts from steps 1-3 + agent_signal_activity
    into concrete dimensions: (a) conviction_calibration — does HIGH
    conviction actually outperform LOW? (b) theme_breadth — do we
    cover only tech/AI or also energy/materials/reshoring? (c)
    loss_discipline — do we catch thesis breaks or ride losers? (d)
    execution_style — average hold days, realized vs intended
    timeframe. (e) agent_balance — any agent gone silent / any
    flooding with low-quality signals. Each dimension should be one
    sentence citing a specific digest number. This REPLACES the
    prior `style_bias_identification` + `agent_hit_rate_audit`."""

    portrait_gap_diagnosis: str = Field(min_length=1)
    """Step 5/DIAGNOSIS. For each dimension in the self-portrait, name
    the IDEAL state (what a medium-long-term value investor with broad
    theme coverage would look like) and the ACTUAL state. Pick the
    top 2-3 highest-leverage gaps — don't try to fix everything.
    Explicitly call out where failures happened: if a theme was
    missed, which agent layer (news vs macro vs tech vs PM) was
    responsible? Attribution is specific, not collective."""

    existing_prompt_audit: str = Field(min_length=1)
    """Step 6/PROMPT AUDIT. For each of the top gaps named in step 5,
    read `agent_prompts_snapshot[{target_agent}]` and enumerate: (a)
    what rules ALREADY exist that address this gap (cite the section /
    heading), (b) whether those existing rules are being followed
    (check corrigibility_trend — are the losses / misses recurring
    despite the rule?), (c) whether there's room for a new rule that
    doesn't conflict with or duplicate existing content. If the
    snapshot shows the target section is saturated with prior
    Learnings, propose a retract-or-replace rather than another
    append. **Do NOT propose a learning without citing what's already
    in the target prompt.**"""

    prompt_edit_reasoning: str = Field(min_length=1)
    """Step 7/PROPOSAL. Given the gaps (step 5) and existing-prompt
    state (step 6), why these specific `proposed_learnings` and not
    others? Corrigibility is the key check: if a cause has been
    worsening for 2 quarters AND the existing prompt has no rule for
    it → append. If a cause has been worsening AND an existing rule
    isn't being followed → DON'T append another (the issue is rule
    adherence, not rule absence); log this as a
    `persistent_blindspot` for the operator to review manually. If
    improving → don't pile on."""


class ThemeCoverage(LLMOutputModel):
    """Quarter-level theme participation — the core "trend capture" metric.

    All four lists may be empty. The meta-reflector populates them from its
    reading of missed_themes + holdings activity during the quarter. Not
    every theme has to appear in every bucket — a theme can be both
    "caught late" and "fully exited", those nuances are in the audit text.
    """
    themes_caught_early: list[str] = []
    """Themes we bought before the move was obvious (entry < 30% of the
    quarter's total move for that theme). The system's genuine alpha."""
    themes_caught_late: list[str] = []
    """Themes we bought after the trend was already priced (entry > 50%
    of total move). Trend-follower rather than trend-identifier
    behavior — ok occasionally, systematically problematic."""
    themes_missed_entirely: list[str] = []
    """Themes that ran ≥20% in the quarter and we never held any symbol
    within. Pure coverage / blindspot failures — the highest-value
    signal for where the system needs to look."""
    emerging_themes_to_watch: list[str] = []
    """Themes forming late in the quarter that didn't run enough to
    show in the caught/missed categories yet. Prior knowledge PM
    should carry into next quarter."""
    mispricing_patterns: list[str] = []
    """Concrete examples where earnings_analyst said bullish+high but
    PM didn't buy, or where macro_analyst tagged a sector tailwind
    and we had no coverage. 1-5 entries, each specific."""


# Mirror of src.models.BuyLossRootCause — quarterly reflector reuses the
# same taxonomy so downstream corrigibility comparisons line up.
MetaLossRootCause = BuyLossRootCause


class LossPattern(LLMOutputModel):
    """One row of loss_pattern_report.top_patterns — cause + attribution +
    proposed guard. Agent attribution drives which prompt gets the
    `proposed_guard` as a candidate learning."""
    root_cause: MetaLossRootCause
    occurrences: int = Field(ge=1)
    total_loss_pct: float
    """Signed sum of pct_move_since_buy for wrongs in this bucket — sign
    preserved so a mix of small/large isn't hidden in absolute values."""
    example_trades: list[str] = Field(min_length=1, max_length=8)
    """Concrete trades "SYMBOL YYYY-MM-DD -X%" so the prompt edit
    justification has anchors, not abstractions."""
    attributable_agent: Literal[
        "tech_analyst", "news_analyst", "macro_analyst",
        "earnings_analyst", "portfolio_manager", "evening_analyst",
        "execution", "no_agent",
    ]
    """`no_agent` when the failure is pure discipline (PM / evening's
    discipline — nothing any individual agent's prompt could have
    caught). `execution` when the issue was broker-side, not LLM."""
    proposed_guard: str = Field(min_length=1, max_length=400)
    """One-sentence candidate prompt addition that would have caught
    this pattern. Empty strings / vague hedges fail validation.
    400 cap is intentionally matched to MissedOpportunity.lesson — the
    LLM cites concrete facts (symbols, dates, pct moves) so terse caps
    force vague language, which is worse than the extra context."""


class LossPatternReport(LLMOutputModel):
    """Quarterly loss autopsy. Parallel structure to ThemeCoverage so the
    meta-reflector's ups/downs analysis stays symmetric."""
    top_patterns: list[LossPattern] = Field(default_factory=list, max_length=5)
    systemic_vs_alpha_split: str = Field(default="")
    """Prose one-liner decomposing losses: "72% alpha-destruction (we
    under-performed the tape), 28% systemic (market also fell)"."""
    worst_single_trade: str | None = None
    """Most painful single wrong BUY this quarter + its root cause +
    whether the pattern is likely to recur. None when no wrongs."""
    corrigibility_score: Literal["improving", "stable", "degrading"] = "stable"
    """Compared to last quarter's report — are the same causes getting
    better, holding, or worse? Drives whether to add more learnings
    (degrading) or give existing ones time to work (improving)."""


class PromptLearning(LLMOutputModel):
    """A proposed edit to one agent's prompt. Append-only for safety —
    never delete existing rules, never rewrite core sections. PR 4's
    prompt_editor enforces additional guards (length, dedup, prohibited
    words, single-quarter rate limits) on top of this schema.

    `retract` is the sole exception to append-only: used in later
    quarters to remove a learning THIS system previously added if the
    subsequent data showed it didn't help.
    """
    agent_name: MetaReflectionAgentName
    operation: Literal["append", "retract"]
    learning_text: str = Field(min_length=20, max_length=200)
    """1-2 concrete sentences. The PR 4 editor rejects entries containing
    "always"/"never"/"override"/"must always"/"must never" as these
    directly conflict with the hard-invariant wording in core prompts."""
    justification: str = Field(min_length=40)
    """Must cite specific digest facts: agent hit-rate numbers, theme
    occurrence counts, loss-cause frequencies, corrigibility deltas.
    A post-hoc model_validator enforces at least one number or '%'
    appears — no vibes-only learnings."""
    retract_target_hash: str | None = None
    """Only set when operation='retract'. Content-hash of the prior
    PromptLearning.learning_text being withdrawn. PR 4 verifies the
    hash matches an actual prior auto-append before deleting."""

    @model_validator(mode="after")
    def _justification_cites_facts(self) -> "PromptLearning":
        # Cheap heuristic — real validator (jaccard / forbidden-word check)
        # lives in the PR 4 prompt_editor. Here we just make sure the LLM
        # didn't emit a justification that's pure adjectives. At minimum
        # some numeric/percent anchor must appear.
        has_digit = any(ch.isdigit() for ch in self.justification)
        if not has_digit:
            raise ValueError(
                "PromptLearning.justification must cite at least one digest "
                "fact with a number (count, %, or quarter period). Got: "
                f"{self.justification[:80]!r}"
            )
        if self.operation == "retract" and not self.retract_target_hash:
            raise ValueError(
                "operation='retract' requires retract_target_hash pointing "
                "to the prior auto-appended learning being withdrawn"
            )
        return self


class QuarterlyMetaReflection(LLMOutputModel):
    """Top-level meta-reflector output. Persisted to
    data/evolution/{period}/reflection.json alongside the digest."""
    period: str
    """e.g. '2026-Q1' — matches the digest's period label."""
    meta_reasoning_chain: MetaReasoningChain
    style_self_portrait: str = Field(default="", max_length=2000)
    """Multi-sentence honest self-description for ongoing audit. Optional:
    `meta_reasoning_chain.self_portrait_synthesis` carries the same
    content as part of the CoT, so some LLM outputs legitimately leave
    this top-level field empty rather than duplicating. When non-empty
    it's useful for downstream continuity rendering."""
    persistent_blindspots: list[str] = Field(default_factory=list, max_length=5)
    root_cause_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    theme_coverage_report: ThemeCoverage
    loss_pattern_report: LossPatternReport
    proposed_learnings: list[PromptLearning] = Field(
        default_factory=list, max_length=3,
    )
    """System enforces max 3 agents edited per quarter AFTER schema
    validation — see PR 4's prompt_editor for the enforcement layer.
    This schema max is the upper bound the LLM sees."""
    confidence: Literal["high", "medium", "low"] = "medium"
    """Meta-confidence — with only 1-2 quarters of data the LLM should
    self-report 'low' and propose at most 1 learning. PR 4's editor
    uses this to scale down edit rates."""
