import logging
import math
import re
import threading
from collections import Counter
from contextlib import contextmanager
from typing import Any, get_origin
from pydantic import BaseModel, TypeAdapter, model_validator

from src.seat_heal import restore_stated_soft_exits
from src.sector_vocab import (  # noqa: F401 — re-exported through `src.models`
    SECTOR_DIRECTIONS,
    SECTOR_STANCE_TO_DIRECTION,
    _ALLOWED_SECTORS,
    _SECTOR_ALIASES,
    normalize_sector_stance,
)
from src.soft_exit_vocab import (  # noqa: F401 — re-exported through `src.models`
    SOFT_EXIT_HEAL_EVENT_REASON,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    SOFT_EXIT_UNKNOWN,
    _SOFT_EXIT_FIELDS,
    missing_stated_falsifier,
    stated_soft_exit,
)

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


#: `specialist_evidence.kind` for the per-stock parse-drop row (board item
#: 158). Defined HERE rather than in `src/pipeline_stages.py` so the read-only
#: API can name the same kind without importing the pipeline — that import is
#: forbidden for `src/api/routes_evidence.py` and enforced by
#: `tests/test_api_safety.py`. `src.pipeline_stages.ANALYSIS_DROP_KIND` is an
#: alias of this constant, not a second copy.
ANALYSIS_DROP_KIND = "analysis_drop"

#: STABLE machine-readable drop codes. The prose reason beside them is written
#: for a person and will be reworded; a reader filtering "show me every stock
#: the tech seat could not read this month" must not be grepping English. These
#: strings are part of the stored record: rename one and every row already on
#: disk becomes unmatchable, so add a new code instead.
#:
#: `malformed_row`   — the model's row was not valid JSON (the #538 salvage
#:                     path); the row never became an object.
#: `schema_invalid`  — the row decoded but failed the Pydantic contract.
#: `unspecified`     — recorded before codes existed, or by a seat whose drop
#:                     site passes no code. NOT an error: a row written by the
#:                     original item-158 fix carries a reason and no code, and
#:                     must still read back.
DROP_CODE_MALFORMED_ROW = "malformed_row"
DROP_CODE_SCHEMA_INVALID = "schema_invalid"
DROP_CODE_UNSPECIFIED = "unspecified"

ANALYSIS_DROP_CODES = frozenset({
    DROP_CODE_MALFORMED_ROW,
    DROP_CODE_SCHEMA_INVALID,
    DROP_CODE_UNSPECIFIED,
})


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
        self._hygiene: Counter = Counter()
        # Denominator for the counter above (item 214, 2026-10-01): a zero
        # violation count is worthless on its own, because it reads the
        # same whether the route was clean or the check never ran. Every
        # answer that is checked is tallied here, keyed by the same
        # provider-tagged model name, so "0 of N" can be stated.
        self._hygiene_observed: Counter = Counter()
        # WHY a row was dropped, keyed the same as `_drops` (model, symbol).
        # Board item 158: the reason used to live only in a log line and the
        # count above, so a later reader could not tell why a name was absent
        # without the log. Kept here so the risk stage can persist it beside
        # the stock it was dropped for (`specialist_evidence`, kind
        # `analysis_drop`). First concrete reason per key wins — a retry's
        # second drop of the same symbol never overwrites the original why.
        #
        # ONE dict holding `(code, prose)` as a pair, not two dicts. A stable
        # code and the human detail that contradicts it is worse than either
        # alone, and two independently-`setdefault`-ed maps can drift the
        # moment one call site passes a reason and the next passes a code.
        # Written once, read as two projections below.
        self._drop_reasons: dict[tuple[str, str], tuple[str, str]] = {}
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

    def record_dropped_item(
        self, model_name: str, key: str, reason: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """A whole parsed item was discarded — `key` is the symbol where known.

        `reason` is the human-readable WHY (e.g. "malformed: ..." or "failed
        validation on ..."); `reason_code` is the STABLE machine-readable
        companion (one of `ANALYSIS_DROP_CODES`), because prose written for a
        person gets reworded and a later reader must still be able to select
        every drop of one kind without grepping English. Passed by the
        technical seat's two drop sites so
        board item 158's requirement — the reason stored alongside the stock,
        not only in the log — can be met downstream. Optional so the other
        seats' drop sites (news, PM, evening) need no change; they record a
        count with no reason, exactly as before.

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
            if reason or reason_code:
                # First concrete reason per key wins; a later retry's drop of
                # the same symbol keeps the original why rather than clobbering
                # it. `setdefault` is inside the same lock as the count, and
                # code and prose are stored as ONE value so they can never be
                # half-updated against each other.
                code = str(reason_code or DROP_CODE_UNSPECIFIED)
                self._drop_reasons.setdefault(
                    (model_name, str(key)), (code, str(reason or "")),
                )

    def record_hygiene_violation(self, model_name: str, kind: str) -> None:
        """The raw answer violated an answer-hygiene rule that a schema
        can't express — fenced markdown around the JSON, or a key the
        schema didn't declare (item 157's runtime check, 2026-09-23,
        replacing the pytest-based live check that no deployed process
        ever holds a real GOOGLE_API_KEY to run: see docs/WORK.md item
        157 and tests/test_tech_schema_live.py). `kind` is a short label
        ("fenced_markdown", "extra_keys") so `describe_hygiene_violations`
        can tell them apart without a second counter to keep in sync.

        This does not fail the row — the row still parses and is used —
        it only means the schema is being followed less than a strict
        response format is supposed to guarantee, which is exactly the
        signal a human deciding item 157's live-enforcement question
        needs and the self-skipping pytest file can never produce.
        """
        if self._suspended:
            return
        with self._lock:
            self._hygiene[(model_name, kind)] += 1

    def record_hygiene_observation(self, model_name: str) -> None:
        """One tech answer was checked for hygiene (item 214, 2026-10-01).

        Counted whether or not it violated anything. Without this, "zero
        violations" and "the check never ran" are the same reading, which
        is exactly the ambiguity item 214 was filed over.
        """
        if self._suspended:
            return
        with self._lock:
            self._hygiene_observed[model_name] += 1

    def hygiene_observed_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._hygiene_observed)

    def total_hygiene_observations(self) -> int:
        with self._lock:
            return sum(self._hygiene_observed.values())

    def describe_hygiene_observations(self) -> str:
        """One-line, grep-able denominator for the operator log."""
        snap = self.hygiene_observed_snapshot()
        if not snap:
            return ""
        return ", ".join(
            f"{model}x{n}" for model, n in sorted(snap.items(), key=lambda kv: -kv[1])
        )

    def snapshot(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._counts)

    def dropped_snapshot(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._drops)

    def hygiene_snapshot(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._hygiene)

    def dropped_reasons_snapshot(self) -> dict[tuple[str, str], str]:
        """WHY each dropped item was dropped, in prose, keyed (model, symbol).

        Only keys with real prose appear: a drop recorded with a code and no
        sentence has nothing to show a person here.
        """
        with self._lock:
            return {
                key: reason for key, (_code, reason) in self._drop_reasons.items()
                if reason
            }

    def dropped_reason_codes_snapshot(self) -> dict[tuple[str, str], str]:
        """The STABLE code for each dropped item, keyed (model, symbol).

        Same source tuple as `dropped_reasons_snapshot`, so the code and the
        prose can never name different causes for the same key.
        """
        with self._lock:
            return {key: code for key, (code, _reason) in self._drop_reasons.items()}

    def total_null_coercions(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def total_dropped(self) -> int:
        with self._lock:
            return sum(self._drops.values())

    def total_hygiene_violations(self) -> int:
        with self._lock:
            return sum(self._hygiene.values())

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._drops.clear()
            self._hygiene.clear()
            self._hygiene_observed.clear()
            self._drop_reasons.clear()

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

    def describe_hygiene_violations(self) -> str:
        """One-line, grep-able summary for the operator log / RM advisory."""
        snap = self.hygiene_snapshot()
        if not snap:
            return ""
        return ", ".join(
            f"{model}.{kind}x{n}"
            for (model, kind), n in sorted(snap.items(), key=lambda kv: -kv[1])
        )


parse_telemetry = AnalysisParseTelemetry()

ACTIONABLE_TECH_RATINGS = frozenset({"buy", "strong_buy", "sell", "strong_sell"})


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
        # back. Never invents a falsifier.
        try:
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
