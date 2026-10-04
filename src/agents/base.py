import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import itertools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.cost_table import estimate_cost, fmt_cost
from src.token_rate import TokenRateGovernor
from src.llm_balance_runway import balance_line as _balance_line
from src import llm_route_journal
from src.cost_circuit import (
    OptionalPaidAnalysisRetrySkipped,
    is_payment_refusal,
    PaidAnalysisSuspended,
    UnavailableLLMCostCircuit,
)

logger = logging.getLogger(__name__)

# The transport policy below base's imports moved VERBATIM into sibling
# modules (src/agents/llm_*.py); every name is re-exported here so existing
# imports and patch targets keep working unchanged.
from src.agents.llm_schema import (  # noqa: F401
    _STRICT_SCHEMA_FALLBACK_LOGGED,
    _RESPONSE_FORMAT_CACHE,
    _strictify_schema,
    _has_free_form_map,
    _response_format_for,
)
from src.agents.llm_providers import (  # noqa: F401
    _OPENAI_PREFIXES,
    _DEEPSEEK_PREFIXES,
    _DEEPSEEK_BASE_URL,
    _OPENROUTER_BASE_URL,
    _GOOGLE_BASE_URL,
    _GOOGLE_PREFIXES,
    _DEEPSEEK_MAX_OUTPUT,
    _DEEPSEEK_DEFAULT_CEILING,
    _is_openai_model,
    _governor_domain_for,
    _is_deepseek_model,
    _is_google_model,
    VALID_PROVIDERS,
    _provider_for,
    resolve_provider,
    _DEFAULT_FALLBACK_PROVIDER,
    _DEFAULT_FALLBACK_MODEL,
)
from src.agents.llm_retry import (  # noqa: F401
    _DEFAULT_MAX_RETRIES,
    _BACKOFF_CAP_S,
    _retry_backoff_seconds,
    _MIN_CAPACITY_BACKOFF_S,
    _LLM_HTTP_TIMEOUT,
    _DEFAULT_RETRY_DEADLINE_S,
    _retry_deadline_s,
    _RETRY_AFTER_CAP_S,
    _retry_after_hint_seconds,
    _FATAL_STATUS_CODES,
    _CAPACITY_STATUS_CODES,
    BACKOFF_FATAL,
    BACKOFF_RETRY_AFTER,
    BACKOFF_JITTER,
    classify_backoff,
    is_capacity_refusal,
    error_aware_backoff_seconds,
    _RETRYABLE_EXC_NAMES,
    _INSUFFICIENT_CREDIT_STATUS,
    _AFFORDABLE_MAX_TOKENS_RE,
    _affordable_max_tokens,
    _is_retryable,
)
from src.agents.llm_route_breaker import (  # noqa: F401
    _ROUTE_COOLDOWN_S,
    _ROUTE_COOLDOWN_MAX_S,
    RouteBreaker,
    _ROUTE_BREAKERS,
    _ROUTE_BREAKERS_LOCK,
    route_breaker_for,
    reset_route_breakers,
    _reset_route_breakers_for_tests,
)
from src.agents.llm_tertiary_route import (  # noqa: F401
    _DEFAULT_TERTIARY_PROVIDER,
    _DEFAULT_TERTIARY_MODEL,
    _DEFAULT_TERTIARY_ALT_PROVIDER,
    _DEFAULT_TERTIARY_ALT_MODEL,
    select_tertiary_route,
    _route_price,
)
from src.agents.llm_concurrency import (  # noqa: F401
    _int_env,
    _OPENAI_MAX_CONCURRENT,
    _OPENAI_LLM_SEMAPHORE,
    _ANTHROPIC_MAX_CONCURRENT,
    _ANTHROPIC_LLM_SEMAPHORE,
    _OPENROUTER_MAX_CONCURRENT,
    _OPENROUTER_LLM_SEMAPHORE,
    _GOOGLE_MAX_CONCURRENT,
    _GOOGLE_LLM_SEMAPHORE,
    _OPENROUTER_TOKENS_PER_MIN,
    _OPENAI_TOKENS_PER_MIN,
    _ANTHROPIC_TOKENS_PER_MIN,
    _GOOGLE_TOKENS_PER_MIN,
    _GOVERNOR_MAX_WAIT_S,
    _GOVERNOR_CHARS_PER_TOKEN,
    _TOKEN_GOVERNORS,
)
from src.agents.llm_attempts import (  # noqa: F401
    _TRUNCATION_FINISH_REASONS,
    LLMEmptyResponseError,
    LLMStreamInterruptedError,
    LLMStreamErrorChunk,
    _max_retries,
    capacity_max_attempts,
    provider_attempt_budget,
)




@dataclass(frozen=True)
class MalformedRow:
    """One element of a list-shaped answer that was not valid JSON.

    `key` is the row's identifying field (e.g. the symbol) read from the
    broken text when it is legible, else None. `reason` is the decoder's own
    message plus the offending line, short enough for one log line.
    """
    key: str | None
    reason: str


@dataclass
class RowSalvage:
    """Result of `AgentResult.parse_json_rows`: every well-formed row, plus
    every row that was present in the answer but malformed. A row in neither
    list was genuinely not returned by the model."""
    rows: list
    malformed: list[MalformedRow]


def _array_element_spans(text: str, open_idx: int) -> tuple[list[tuple[int, int | None]], int]:
    """Spans of the top-level `{...}` elements of the array opening at
    `open_idx`, and the index just past the array's close (or len(text)).

    String-aware bracket counting, with one recovery rule: string state is
    reset at every raw newline, because JSON forbids a literal newline inside
    a string (RFC 8259 section 7: control characters U+0000-U+001F must be
    escaped). A stray or missing quote therefore corrupts at most its own
    line, never the element boundaries of the rows after it. An element still
    open when the text ends is returned with end None (a cut-off answer).
    """
    spans: list[tuple[int, int | None]] = []
    depth = 0
    in_str = False
    escaped = False
    elem_start: int | None = None
    n = len(text)
    pos = open_idx + 1
    while pos < n:
        ch = text[pos]
        if ch == "\n":
            in_str = False
            escaped = False
        elif in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0 and ch == "{":
                elem_start = pos
            depth += 1
        elif ch in "}]":
            if depth == 0:
                if ch == "]":
                    return spans, pos + 1
                # Stray closer at array level: ignore it.
            else:
                depth -= 1
                if depth == 0 and elem_start is not None:
                    spans.append((elem_start, pos + 1))
                    elem_start = None
        pos += 1
    if elem_start is not None:
        spans.append((elem_start, None))
    return spans, n


@dataclass
class AgentResult:
    raw_text: str
    tokens_used: int
    model: str
    user_message: str = ""
    # Per-call cost tracking — populated by `run()` when the model's
    # pricing is known in `src/cost_table.py`. None when the model name
    # isn't in the pricing table; callers must NOT default to 0 in that
    # case (would silently understate aggregate cost). Split input/output
    # token counts retained so cost can be recomputed if pricing changes
    # post-hoc.
    input_tokens: int = 0
    output_tokens: int = 0
    # Board item 188 (RECORDING ONLY): the machine-readable reason this
    # seat's OWN acceptance gate refused the answer, set by the seat on its
    # rejection path and persisted to `agent_logs.acceptance_reason`. None
    # means the answer was used, or that this seat does not yet name its
    # reasons — never "accepted".
    gate_reason: str | None = None
    cost_usd: float | None = None
    # Provider stop/finish reason + a derived flag. `truncated` is True when
    # the model hit the token ceiling mid-output (Anthropic stop_reason
    # 'max_tokens' / OpenAI finish_reason 'length'). This is distinct from a
    # clean "no signal" answer: a PM decision cut off at max_tokens parses to
    # None and looks identical to "chose not to trade" — callers/notifier can
    # now tell a swallowed truncation from a real silence.
    finish_reason: str | None = None
    truncated: bool = False
    # Stage 1 (QAMC provider/model/correlation plumbing) attribution fields.
    # requested_* is what THIS call was configured to use; model/actual_provider
    # is what actually answered (may differ on cross-provider failover — see
    # used_fallback). Never conflate the two: DECISION #12 forbids counting a
    # fallback as the requested provider/model.
    requested_model: str = ""
    requested_provider: str = ""
    actual_provider: str = ""
    used_fallback: bool = False
    # First 12 hex chars of sha256(system_prompt) — a cheap, stable "did the
    # prompt text change between two calls" signal, not a semantic version.
    prompt_version: str = ""
    latency_s: float = 0.0
    # Provider HTTP requests represented by this logical result. Normally one;
    # retry/failover and Tech chunk aggregation can be greater.
    provider_requests: int = 1
    # Transport-valid output may still fail a deterministic semantic contract.
    semantic_status: str | None = None
    semantic_error: str | None = None

    # Top-level keys we recognize as "this looks like a real agent output."
    # When the LLM prose includes an extra JSON fragment (self-correction,
    # partial thinking-out-loud, or a tool-like object), these anchors let us
    # pick the actual output instead of the largest stray fragment.
    _EXPECTED_AGENT_KEY_WEIGHTS = {
        "decisions": 50,           # PortfolioDecision (legacy pre-constructor key)
        # Phase-2 constructor refactor renamed PM's actionable output from
        # `decisions` to `targets` but this table was never updated, so a
        # full PortfolioDecision scored only 40 (portfolio_view +
        # reasoning_chain) while its own inner `targets` ARRAY scored
        # 5/symbol — any plan with ≥8 targets lost to a fragment of itself
        # and the whole morning collapsed to "no trades" (2026-08-17/20
        # production: 10 of 13 decision runs died here; reproduced from
        # recorded payloads in tests/fixtures/pm_response_*).
        "targets": 50,             # PortfolioDecision (current key)
        "approved": 50,            # RiskVerdict
        "actions": 50,             # MiddayReview
        "daily_summary": 40,       # EveningReport
        "tomorrow_outlook": 40,    # EveningReport alt anchor
        "regime": 40,              # MacroAnalysis
        "investment_implications": 40,  # EarningsAnalysis
        "macro_narrative": 40,     # NewsIntelligenceReport
        "analyses": 40,            # TechAnalyst batch wrapper
        "findings": 50,            # SmartMoney synthesis wrapper
        "portfolio_view": 20,      # PortfolioDecision summary
        "reasoning_chain": 20,     # nested rationale wrapper
        "symbol": 5,               # TechAnalysisResult single
        "rating": 5,               # TechAnalysisResult single
    }

    @staticmethod
    def _shape_score(parsed) -> int:
        """How 'agent-output shaped' a JSON candidate looks. Higher is better."""
        # A top-level LIST is a first-class agent shape: tech_analyst returns
        # an array of per-symbol analyses (tech_analyst.py: `items = parsed if
        # isinstance(parsed, list) else [parsed]`). Scoring it 0 meant that
        # whenever the model wrapped the array in ANY prose (so the clean
        # json.loads happy path missed), the candidate scan compared the array
        # (score 0) against each of its own elements (score > 0) and returned
        # the LAST ELEMENT — silently discarding every other symbol's analysis
        # in the chunk. Score the container by the SUM of its elements so it
        # strictly outranks any single element it contains (2026-07-16 audit;
        # reproduced: a 3-analysis array returned 1 dict).
        if isinstance(parsed, list):
            return sum(AgentResult._shape_score(item) for item in parsed)
        if not isinstance(parsed, dict):
            return 0
        keys = set(parsed.keys())
        return sum(
            weight
            for key, weight in AgentResult._EXPECTED_AGENT_KEY_WEIGHTS.items()
            if key in keys
        )

    @staticmethod
    def _repair_unquoted_keys(text: str) -> str:
        """Fix one specific, narrow JSON syntax defect: an object key missing
        its OPENING quote while the closing quote survives, e.g.
        `  pm_briefing": "value"` instead of `  "pm_briefing": "value"`.

        Confirmed from a real production payload (news_analyst,
        2026-09-02 19:30 ET — see docs/INCIDENT_HISTORY.md): the model
        dropped exactly one opening quote on an interior top-level key.
        Because Anthropic's pretty-printed JSON always starts a key on its
        own line, and a JSON string can never contain a literal newline, any
        line beginning with a bare identifier immediately followed by `":`
        is unambiguously a key position — never the middle of a string
        value. Deliberately does NOT attempt to repair anything else
        (trailing commas, unescaped quotes inside values, etc.) — those are
        different defects with different failure signatures we have not
        observed in production; guessing a fix for an unobserved failure
        mode is exactly what docs/WORK.md's DATA QUALITY AUDIT warned
        against.
        """
        return re.sub(
            r'(?m)^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)":',
            r'\1"\2":',
            text,
        )

    def parse_json(self) -> dict | list | None:
        text = self.raw_text.strip()
        try:
            parsed = json.loads(text)
            # Full-text parse wins outright if it's a dict/list; no candidate
            # search needed. This is the happy path — LLM returned clean JSON.
            return parsed
        except json.JSONDecodeError:
            pass

        # One narrow, evidence-backed repair attempt on the full text before
        # falling back to fragment scanning below. A repaired FULL parse
        # recovers the whole report; the fragment scanner below can only
        # ever recover the largest surviving PIECE of a broken object —
        # exactly what turned a single missing quote character into a
        # "4 required top-level fields missing" structural failure in
        # production (2026-09-02).
        try:
            parsed = json.loads(self._repair_unquoted_keys(text))
            return parsed
        except json.JSONDecodeError:
            pass

        # Each candidate carries its source SPAN (start, end in raw_text) so
        # nested fragments can be recognized. (score, size, idx, span, parsed)
        candidates: list[tuple[int, int, int, tuple[int, int], dict | list]] = []
        # idx preserves source order so we can break ties predictably.
        idx = 0
        # Fenced ```json blocks — highest trust.
        for match in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", self.raw_text, re.DOTALL):
            fenced = match.group(1).strip()
            try:
                parsed = json.loads(fenced)
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(self._repair_unquoted_keys(fenced))
                except json.JSONDecodeError:
                    continue
            candidates.append((
                self._shape_score(parsed), len(json.dumps(parsed)), idx,
                match.span(1), parsed,
            ))
            idx += 1

        decoder = json.JSONDecoder()
        for i, ch in enumerate(self.raw_text):
            if ch not in "{[":
                continue
            try:
                parsed, end = decoder.raw_decode(self.raw_text[i:])
            except json.JSONDecodeError:
                continue
            candidates.append((
                self._shape_score(parsed), len(json.dumps(parsed)), idx,
                (i, i + end), parsed,
            ))
            idx += 1

        # Nested-fragment filter: a candidate STRICTLY contained inside a
        # larger candidate is part of that candidate's content, not a later
        # "correction" of it — the recency tie-break below was designed for
        # DISJOINT draft-then-fix fragments. Without this, the PM's inner
        # `targets` array (5 pts/symbol) outranked the very object that
        # contained it once the plan reached ≥8 names, and the entire
        # morning decision was destroyed by a fragment of itself
        # (2026-08-17/20 production incident; see _EXPECTED_AGENT_KEY_WEIGHTS
        # note). A container that itself looks like agent output (score > 0)
        # therefore always wins over its own fragments, REGARDLESS of the
        # fragments' scores. The only nested fragment worth keeping is one
        # inside a score-0 container — the prose-wrapper case
        # (e.g. {"thinking": ..., "answer": {...}}), where the wrapper has
        # no recognizable agent shape and the payload is the real output.
        def _strictly_inside(inner: tuple[int, int], outer: tuple[int, int]) -> bool:
            return (
                outer[0] <= inner[0] and inner[1] <= outer[1]
                and (outer[0] < inner[0] or inner[1] < outer[1])
            )

        filtered = [
            c for c in candidates
            if not any(
                other is not c
                and other[0] > 0
                and _strictly_inside(c[3], other[3])
                for other in candidates
            )
        ]
        candidates = filtered or candidates

        if candidates:
            max_shape = max(item[0] for item in candidates)
            if max_shape > 0:
                # Once something looks like a real agent output, prefer the
                # latest correction over an earlier larger draft.
                shaped = [item for item in candidates if item[0] == max_shape]
                return max(shaped, key=lambda item: (item[2], item[1]))[4]

            # If nothing has recognizable agent keys, fall back to the largest
            # valid JSON fragment and use recency only as a tiebreaker.
            return max(candidates, key=lambda item: (item[1], item[2]))[4]

        logger.warning("Failed to parse agent response as JSON: %s", self.raw_text[:200])
        return None

    def parse_json_rows(
        self, key_field: str = "symbol", list_field: str | None = None,
    ) -> RowSalvage | None:
        """Row-by-row parse for an agent whose answer is a LIST of objects.

        Opt-in; `parse_json` above is unchanged for every other seat. The
        difference only shows when the whole answer is not valid JSON:
        `parse_json` then returns ONE winning fragment, which for a list
        answer means one garbled row throws away every well-formed row beside
        it (tech seat, 2026-09-17 14:31 `intra_check-26f52bf2`: five rows
        returned, one carried a bare `n/a`, one row survived; the retry lost
        ORCL and ETN the same way although both answers carried them
        well-formed). Here each element of the answer's array is parsed on
        its own: good rows are kept, broken ones are reported as
        `MalformedRow` with their `key_field` value when legible.

        `list_field`, when given (item 157): the answer is expected to be a
        wrapper OBJECT (e.g. `{"results": [...]}` — see `TechAnalystAnswer`
        in src/models.py) rather than a bare array, because a strict
        provider-side response schema must root at an object. When the full
        (or repaired) text parses to a dict carrying that key as a list, that
        list is what gets salvaged row-by-row. A bare list is still accepted
        as-is (a legacy stored answer, or a route that didn't honour the
        schema), and a dict without that key falls back to the pre-existing
        "treat the whole object as one row" behaviour — this method never
        gets stricter than it used to for a caller that passes no
        `list_field`.

        Returns None only when nothing in the answer parses at all.
        """
        def _rows_from(parsed):
            if (
                list_field is not None
                and isinstance(parsed, dict)
                and isinstance(parsed.get(list_field), list)
            ):
                return parsed[list_field]
            if (
                list_field is not None
                and isinstance(parsed, dict)
                and key_field not in parsed
            ):
                # Adversary review, 2026-09-23: a model can answer with a
                # valid JSON OBJECT under the WRONG key — a differently
                # named wrapper ("signals", "analysis") or a null under the
                # right key with the real array under another
                # ("results": null, "data": [...]). The prompt asking for a
                # bare array made this impossible; 243 real answer-chunks
                # replayed from a read-only production snapshot
                # (2026-08-17 to 2026-09-18) are all bare arrays, so this
                # path was never exercised before the wrapper schema
                # (see tests/test_tech_seat_production_replay.py). Prefer
                # any SINGLE list-of-dicts value found at the TOP LEVEL of
                # the object over treating the whole object as one row —
                # the old behaviour silently drops every real candidate in
                # the chunk with no per-symbol reason.
                #
                # `key_field not in parsed` is required first, and each
                # candidate's elements must themselves carry `key_field`
                # (2nd adversary pass, 2026-09-23): without both checks, a
                # perfectly normal SINGLE-ROW answer that happens to carry
                # any nested list-of-objects field of its own (a model
                # answering `{"symbol": "SPY", ..., "levels": [{"price":
                # 1}]}` — a shape this schema doesn't ask for today, but
                # nothing stops a future field from looking like it) would
                # have its real row thrown away in favour of that unrelated
                # nested list. Requiring `key_field` on both sides means
                # this only ever fires for something that actually looks
                # like a differently-keyed list OF ROWS, never for a
                # single row that happens to nest a list.
                #
                # When more than one such candidate exists there is no way
                # to tell which is the real one without guessing, so this
                # deliberately falls through to the same conservative
                # whole-object behaviour as before rather than picking one.
                list_candidates = [
                    v for v in parsed.values()
                    if isinstance(v, list) and v
                    and all(
                        isinstance(e, dict) and key_field in e for e in v
                    )
                ]
                if len(list_candidates) == 1:
                    return list_candidates[0]
            return parsed if isinstance(parsed, list) else [parsed]

        text = self.raw_text.strip()
        for candidate in (text, self._repair_unquoted_keys(text)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return RowSalvage(rows=_rows_from(parsed), malformed=[])

        # Every array in the answer whose first element is an object. The
        # LAST one wins, matching parse_json's "latest correction" rule for a
        # draft-then-fix answer.
        #
        # When `list_field` is given, restrict the scan to arrays that are
        # actually the VALUE of that key (`"results": [...]`) when at least
        # one such labelled array is found. Item 157 adversary review
        # (2026-09-20): without this, a broken answer that happens to carry
        # any OTHER array-of-objects after `results` in the raw text — a
        # sibling key, a nested field, a stray self-correction fragment —
        # would win outright under the old "last array of objects anywhere"
        # rule, silently discarding the real rows. Falls back to the
        # unrestricted scan when no labelled array is found at all (a bare
        # legacy list answer, or a route that ignored the schema).
        label_positions: list[int] = []
        if list_field is not None:
            label_re = re.compile(
                r'"%s"\s*:\s*(?=\[)' % re.escape(list_field),
            )
            label_positions = [m.end() for m in label_re.finditer(self.raw_text)]

        def _scan_from(start_positions: list[int] | None) -> list[tuple[int, int | None]] | None:
            chosen: list[tuple[int, int | None]] | None = None
            search = 0
            while True:
                if start_positions is not None:
                    remaining = [p for p in start_positions if p >= search]
                    if not remaining:
                        break
                    open_idx = min(remaining)
                else:
                    open_idx = self.raw_text.find("[", search)
                    if open_idx < 0:
                        break
                nxt = open_idx + 1
                while nxt < len(self.raw_text) and self.raw_text[nxt] in " \t\r\n":
                    nxt += 1
                if nxt >= len(self.raw_text) or self.raw_text[nxt] != "{":
                    search = open_idx + 1
                    continue
                spans, search = _array_element_spans(self.raw_text, open_idx)
                if spans:
                    chosen = spans
            return chosen

        chosen = _scan_from(label_positions) if label_positions else None
        if chosen is None:
            chosen = _scan_from(None)

        if chosen is None:
            # Not list-shaped (e.g. a single object in prose, or a wrapper
            # object whose `list_field` array itself contains no top-level
            # `[{` — an empty `"results": []` for instance): the shared
            # fragment parser's answer, still unwrapped via `list_field`.
            parsed = self.parse_json()
            if parsed is None:
                return None
            return RowSalvage(rows=_rows_from(parsed), malformed=[])

        key_re = re.compile(r'"%s"\s*:\s*"([^"\n]+)"' % re.escape(key_field))
        rows: list = []
        malformed: list[MalformedRow] = []
        for start, end in chosen:
            chunk = self.raw_text[start:end] if end is not None else self.raw_text[start:]
            key_match = key_re.search(chunk)
            key = key_match.group(1) if key_match else None
            if end is None:
                malformed.append(MalformedRow(
                    key, "row cut off: the answer ended before the row closed",
                ))
                continue
            error: json.JSONDecodeError | None = None
            for candidate in (chunk, self._repair_unquoted_keys(chunk)):
                try:
                    rows.append(json.loads(candidate))
                    error = None
                    break
                except json.JSONDecodeError as exc:
                    error = error or exc
            if error is not None:
                lines = chunk.splitlines()
                bad_line = lines[error.lineno - 1].strip() if 0 < error.lineno <= len(lines) else ""
                malformed.append(MalformedRow(
                    key, f"{error.msg} near {bad_line!r}",
                ))
        if not rows and not malformed:
            return None
        return RowSalvage(rows=rows, malformed=malformed)


def usage_telemetry_word(input_tokens, output_tokens, cost_usd,
                         provider_requests) -> str | None:
    """Did the provider's answer carry usage information? — recorded, never inferred.

    A response with no token counts used to be stored as 0 tokens and a NULL
    cost, which reads the same as a measured zero. This names the case:
    "complete" (tokens and cost known), "no_cost" (tokens known, no price),
    "no_usage" (no token counts at all). None when no provider request was made
    or the values are not numbers (legacy/replay fixtures): unknown, not guessed.
    Recording only; nothing reads it to decide anything.
    """
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if provider_requests == 0 or not (num(input_tokens) and num(output_tokens)):
        return None
    if input_tokens == 0 and output_tokens == 0:
        return "no_usage"
    return "complete" if num(cost_usd) else "no_cost"


def agent_log_kwargs(result: AgentResult) -> dict:
    """Common Stage 1 telemetry kwargs for Database.insert_agent_log(),
    derived from an AgentResult. Callers add agent_name/run_id/decision_id
    and the summary/response fields on top. Centralized so all nine call
    sites stay consistent instead of re-deriving `status` etc. independently."""
    def text_or_none(value):
        return value if isinstance(value, str) and value else None

    provider_requests = getattr(result, "provider_requests", 1)
    if (not isinstance(provider_requests, int)
            or isinstance(provider_requests, bool)
            or provider_requests < 0):
        # Compatibility for old test/replay fixtures built as open-ended
        # MagicMocks and for any legacy caller that predates this field.
        provider_requests = 1
    semantic_status = text_or_none(getattr(result, "semantic_status", None))
    used_fallback = getattr(result, "used_fallback", False) is True
    latency = getattr(result, "latency_s", None)
    if not isinstance(latency, (int, float)) or isinstance(latency, bool):
        latency = None
    truncated = getattr(result, "truncated", None)
    if not isinstance(truncated, bool):
        truncated = None
    return dict(
        telemetry=usage_telemetry_word(
            getattr(result, "input_tokens", None),
            getattr(result, "output_tokens", None),
            getattr(result, "cost_usd", None),
            provider_requests,
        ),
        requested_provider=text_or_none(getattr(result, "requested_provider", None)),
        requested_model=text_or_none(getattr(result, "requested_model", None)),
        actual_provider=text_or_none(getattr(result, "actual_provider", None)),
        prompt_version=text_or_none(getattr(result, "prompt_version", None)),
        latency_s=latency,
        provider_requests=provider_requests,
        status=(semantic_status or ("fallback" if used_fallback else "success")),
        finish_reason=text_or_none(getattr(result, "finish_reason", None)),
        truncated=truncated,
    )


def seat_acceptance_kwargs(refusal_reason: str | None, result=None) -> dict:
    """Did the SEAT accept its own model's answer? — the fact `status` never held.

    `status` says the provider call returned. It says nothing about whether
    the seat could use what came back, which is why 15/15 portfolio-manager
    rows and 19/55 risk-manager rows sit at "success" holding bodies that are
    not the seat's format at all, and why no usable-answer rate is computable
    for any model today. Pass the refusal reason the call site ALREADY has on
    its rejection path, or None when the answer was used.

    Board item 188: pass the seat's `AgentResult` too and, when the gate
    that refused set its own machine-readable `gate_reason`, THAT word is
    stored instead of the call site's one-word-per-seat summary. The three
    decision seats reject for several different causes and most of those
    causes previously survived only as prose in a log line; the column must
    carry the reason the gate itself produced, not a restatement of "it
    failed". An absent or non-string `gate_reason` falls back to the call
    site's word — never to a guess.

    Recording only. This decides nothing and changes nothing: a seat that
    refuses an unusable answer behaves exactly as it did before. Nothing
    reads these two columns back into a sizing, stop, exit or routing
    decision, and they must never be swept for a threshold.
    """
    from src.refusal_signature import (
        SEAT_ACCEPTED, SEAT_REFUSED, SEAT_REFUSAL_REASONS,
    )
    if not refusal_reason:
        return {"acceptance": SEAT_ACCEPTED, "acceptance_reason": None}
    gate_reason = getattr(result, "gate_reason", None)
    reason = (
        gate_reason if isinstance(gate_reason, str) and gate_reason
        else str(refusal_reason)
    )
    if reason not in SEAT_REFUSAL_REASONS:
        # An unregistered word must not silently enter the column: record the
        # refusal (true, and the load-bearing half) and flag the reason rather
        # than inventing vocabulary.
        reason = "unregistered:" + reason
    return {"acceptance": SEAT_REFUSED, "acceptance_reason": reason}


def _build_llm_client(provider: str, api_key: str):
    """Construct the SDK client for `provider`, given its API key.

    Shared by BaseAgent.__init__ (the primary client) and _try_failover (the
    fallback client) so the two can never diverge in how a provider's client
    is built — before this helper existed, __init__ had its own inline
    if/elif chain and a hardcoded Anthropic construction lived separately
    inside _try_failover, and the two had already started drifting (the
    failover path never got DeepSeek/OpenRouter/relay/CA-bundle handling).

    max_retries=0 on EVERY client built here: both SDKs default to 2 internal
    retries on 429/5xx (incl. the relay's CF 524) with their own backoff,
    silently turning each _execute() attempt into ~3 HTTP calls and
    invalidating the retry-budget math documented at _DEFAULT_MAX_RETRIES.
    The agent-level loop in _execute() is the SINGLE owner of retry policy.
    """
    if provider == "deepseek":
        # OpenAI-compatible endpoint at a custom base_url with the DeepSeek key.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_DEEPSEEK_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "openrouter":
        # Also OpenAI-API-compatible — identical shape to the DeepSeek branch
        # above, just a different base_url + key. Reuses _openai_wire_call()
        # unmodified (see there): zero new call code.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "google":
        # Google AI Studio's OpenAI-compatible endpoint — same shape again.
        # The credential is injected as `Authorization: Bearer {value}`,
        # which the OpenAI SDK's `api_key=` already sends natively.
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=_GOOGLE_BASE_URL,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    if provider == "openai":
        from openai import OpenAI
        # OPENAI_BASE_URL lets OpenAI traffic go through an OpenAI-compatible
        # relay/proxy (a "中转站") instead of api.openai.com — same chat/
        # completions wire format, just a different host + key. Empty/unset
        # => the SDK's default (api.openai.com). Read explicitly (not via the
        # SDK's own env magic) so it's visible + testable. The base_url must
        # include the API path prefix the relay serves (e.g. .../v1).
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        # OPENAI_CA_BUNDLE: trust a private CA for the relay's HTTPS (e.g. a
        # relay behind Caddy's internal CA, whose self-signed chain the
        # public trust store rejects). Path to a PEM of trust anchors (the
        # relay's root CA). We still FULLY verify — cert chain + hostname/IP
        # (the relay's leaf carries the IP in its SAN) — just against this CA
        # instead of certifi. Scoped to the OpenAI client ONLY; every other
        # provider's client keeps the public trust store. Unset => default
        # public CAs.
        ca_bundle = os.environ.get("OPENAI_CA_BUNDLE", "").strip()
        if ca_bundle:
            import httpx
            return OpenAI(
                api_key=api_key, base_url=base_url,
                http_client=httpx.Client(verify=ca_bundle, timeout=_LLM_HTTP_TIMEOUT),
                max_retries=0,
            )
        return OpenAI(api_key=api_key, base_url=base_url,
                      timeout=_LLM_HTTP_TIMEOUT, max_retries=0)
    # anthropic (default)
    from anthropic import Anthropic
    return Anthropic(api_key=api_key, timeout=_LLM_HTTP_TIMEOUT, max_retries=0)


class BaseAgent(ABC):
    # Unit tests that exercise isolated provider parsing may opt out through
    # tests/conftest.py. Production code never changes this flag: a paid call
    # without the mandatory persistent breaker is a fail-closed error.
    _allow_unmetered_for_tests = False

    # The pydantic model this agent's JSON response validates against, when
    # one exists — used ONLY to build the OpenRouter `response_format`
    # (json_schema) request extra. `None` (the default) means "this agent's
    # output has no fixed top-level schema to declare" and the OpenRouter
    # call is sent unformatted, exactly as before. Set by subclasses; see
    # each subclass's own comment for why that model was chosen.
    result_model: type | None = None

    def __init__(self, api_key: str, model: str, max_tokens: int = 4096,
                 fallback_api_key: str = "", provider: str | None = None,
                 provider_order: list[str] | None = None,
                 fallback_provider: str = _DEFAULT_FALLBACK_PROVIDER,
                 fallback_model: str = _DEFAULT_FALLBACK_MODEL,
                 tertiary_api_key: str = "",
                 tertiary_provider: str = _DEFAULT_TERTIARY_PROVIDER,
                 tertiary_model: str = _DEFAULT_TERTIARY_MODEL,
                 tertiary_alt_api_key: str = "",
                 tertiary_alt_provider: str = _DEFAULT_TERTIARY_ALT_PROVIDER,
                 tertiary_alt_model: str = _DEFAULT_TERTIARY_ALT_MODEL,
                 reasoning_effort: str = "medium",
                 structured_output: bool = True):
        self.model = model
        self.max_tokens = max_tokens
        # Uniform-testing settings (owner requirement, 2026-09-14): every
        # seat calling through OpenRouter is asked for the SAME explicit
        # reasoning budget and, when this agent declares a `result_model`,
        # the SAME structured-output constraint — see _openai_wire_call.
        self._reasoning_effort = reasoning_effort
        self._structured_output = structured_output
        # OpenRouter endpoint preference for this seat — see
        # LLMConfig.<agent>_provider_order. Ordered most-preferred first, with
        # fallbacks left ENABLED: every endpoint for a given model id serves
        # the same weights, so falling through to a pricier one costs money,
        # not correctness, whereas pinning `only` would fail the seat closed
        # over a price tier. Cost stays truthful because OpenRouter reports
        # the actual charge per call (see _openai_wire_call).
        self._provider_order = list(provider_order) if provider_order else None
        # `provider` is the Stage 1 explicit override (config.llm.<agent>_provider);
        # None (the default for every pre-Stage-1 config) falls through to the
        # unchanged prefix-inference chain — see resolve_provider().
        self._provider = resolve_provider(model, provider)
        self._use_deepseek = self._provider == "deepseek"
        self._use_openai = self._provider == "openai"
        self._use_openrouter = self._provider == "openrouter"
        self._use_google = self._provider == "google"
        # Cross-provider failover target — configurable (see
        # _DEFAULT_FALLBACK_PROVIDER/_DEFAULT_FALLBACK_MODEL above). Resolved
        # through the same resolve_provider() helper as the primary so a
        # typo'd `fallback_provider` can't silently misroute, exactly like
        # every other provider field in this codebase.
        self._fallback_provider = resolve_provider(fallback_model, fallback_provider)
        self._fallback_model = fallback_model
        # Failover credential. Empty => failover disabled outright regardless
        # of the pair check below. Passing it when the fallback pair happens
        # to equal the primary's is harmless, NOT an error — run() no-ops the
        # failover attempt in that case (see _failover_reachable).
        self._fallback_api_key = (fallback_api_key or "").strip()
        # The failover gate, computed once: a fallback key is configured AND
        # the fallback (provider, model) pair is not identical to the
        # primary's — never fail over onto the exact thing that just failed.
        # This generalizes the old "primary is not itself Anthropic" special
        # case (which only made sense when the ONLY fallback target was
        # Anthropic): a Claude primary can now legitimately fail over too, as
        # long as the configured fallback isn't Claude-to-Claude. Mirrored
        # exactly in AppConfig._check_provider_attempt_budget /
        # _check_llm_provider_keys (src/config.py) — those two independently
        # computing this and drifting apart is what caused the 2026-08-31
        # outage (see provider_attempt_budget's docstring).
        self._failover_reachable = bool(self._fallback_api_key) and (
            (self._fallback_provider, self._fallback_model)
            != (self._provider, self.model)
        )
        # === Route 3: a genuinely DIFFERENT model ===========================
        # See _DEFAULT_TERTIARY_PROVIDER above for why haiku-over-OpenRouter
        # and not either direct endpoint (neither is credentialed on this
        # deployment). Reachability is gated exactly like the failover's:
        # a key must be configured AND the pair must differ from BOTH routes
        # already in the ladder, or route 3 is just a third attempt at
        # something that has already failed twice.
        #
        # Before any of that is fixed, route 3 may be SWAPPED for the
        # second-road substitute — see `_DEFAULT_TERTIARY_ALT_PROVIDER` and
        # `select_tertiary_route` above for the rule and the 2026-09-29
        # measurement behind it. The swap fires only for a seat whose routes
        # 1 and 2 have collapsed onto one provider; it never adds a rung.
        _configured_tertiary = (
            resolve_provider(tertiary_model, tertiary_provider), tertiary_model,
        )
        _alt_key = (tertiary_alt_api_key or "").strip()
        _alt = (
            (resolve_provider(tertiary_alt_model, tertiary_alt_provider),
             tertiary_alt_model)
            if _alt_key and (tertiary_alt_model or "").strip()
            and (tertiary_model or "").strip()
            else None
        )
        _selected = select_tertiary_route(
            primary=(self._provider, self.model),
            fallback=((self._fallback_provider, self._fallback_model)
                      if self._failover_reachable else None),
            tertiary=_configured_tertiary,
            alt=_alt,
        )
        self._tertiary_on_alt_road = _selected is _alt and _alt is not None
        self._tertiary_provider, self._tertiary_model = _selected
        self._tertiary_api_key = (
            _alt_key if self._tertiary_on_alt_road
            else (tertiary_api_key or "").strip()
        )
        self._tertiary_reachable = bool(self._tertiary_api_key) and (
            (self._tertiary_provider, self._tertiary_model)
            not in {(self._provider, self.model),
                    (self._fallback_provider, self._fallback_model)}
        )
        # Process-wide half-open breaker for THIS primary pair. Shared with
        # every other seat configured to the same primary — see RouteBreaker.
        self._route_breaker = route_breaker_for(self._provider)
        # Route 2 gets a breaker too. Without one the demotion logic is
        # asymmetric and only ever protects the FREE road: if OpenRouter is
        # the thing that is down, every call would pay two primary attempts
        # plus a doomed secondary plus a tertiary, forever. When routes 2 and
        # 3 share a provider (they do by default — see
        # _DEFAULT_TERTIARY_PROVIDER's honest-residual note) this is the SAME
        # breaker object, which is correct: one account, one failure domain.
        self._fallback_breaker = route_breaker_for(self._fallback_provider)
        self._tertiary_breaker = route_breaker_for(self._tertiary_provider)

        # Attached after TradingPipeline initializes the shared SQLite DB.
        self._cost_circuit = None

        self.client = _build_llm_client(self._provider, api_key)

    def set_cost_circuit(self, circuit) -> None:
        """Attach the mandatory process-shared paid-analysis gate."""

        self._cost_circuit = circuit

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        ...

    @abstractmethod
    def build_user_message(self, **kwargs) -> str:
        ...

    def run(self, **kwargs) -> AgentResult:
        user_message = self.build_user_message(**kwargs)
        return self._execute(user_message)

    def repair_reprompt(self, failed: AgentResult, error, schema_name: str) -> AgentResult:
        """One bounded re-ask after a response parsed as JSON but failed
        schema validation (e.g. a mandatory reasoning_chain field omitted).

        Production incident 2026-08-18 15:03: the Risk Manager APPROVED the
        day's plan with three sensible halving modifications, but omitted
        `sizing_sanity` and `overall` from its reasoning_chain — validation
        failed, the verdict became None, and RiskStage recorded
        "REJECTED: parse error". A prose omission silently destroyed an
        approving verdict and ended the trading day. One corrective call
        (~$0.002) names the exact validation errors and asks the model to
        complete the object; if the second attempt also fails, callers
        keep their existing fail-closed path (None → reject/retry).

        This is SCHEMA COMPLETION, never re-decision. The coda explicitly
        scopes the model to filling missing/invalid narrative fields and
        forbids touching any decision-bearing value. That instruction is
        advisory, not enforcement — callers MUST additionally verify the
        repaired parse's decision-bearing fields are byte-identical to the
        pre-repair parse (see `RiskManagerAgent`/`PortfolioManagerAgent`)
        and fail closed if the model changed them or the original failure
        was itself rooted in a decision-bearing field (in which case
        callers should skip repair entirely — see
        `validation_error_touches`).

        Deliberately built on `_execute` (not `run`) so the original
        user message is replayed verbatim with a repair coda. The
        returned AgentResult's `user_message` includes the coda, so the
        agent_logs row is self-describing about being a repair call.
        """
        coda = (
            f"\n\n## SCHEMA REPAIR REQUIRED — NOT A RE-DECISION\n"
            f"Your previous response was parseable JSON but failed "
            f"{schema_name} schema validation.\n\n"
            f"Validation errors:\n{error}\n\n"
            f"Your previous response was:\n{failed.raw_text}\n\n"
            f"Re-emit the COMPLETE JSON object, filling in ONLY the "
            f"missing or invalid schema field(s) named in the validation "
            f"errors above — typically an empty or omitted narrative "
            f"field. Do NOT reconsider, add, remove, or change the value "
            f"of any decision-bearing field: every target/symbol/weight, "
            f"every approved/modifications/rejected_symbols/scale_all_buys/"
            f"reason_category value must be returned EXACTLY as in your "
            f"previous response. "
            f"This is schema completion only. Respond ONLY with the JSON "
            f"object."
        )
        logger.warning(
            "Agent %s: %s validation failed — attempting one repair reprompt",
            self.name, schema_name,
        )
        return self._execute(failed.user_message + coda, retry_kind="schema_repair")

    @staticmethod
    def validation_error_touches(error, field_names: tuple[str, ...]) -> bool:
        """True if a pydantic ValidationError is rooted at one of the
        named top-level fields.

        Used to skip `repair_reprompt` entirely when the validation
        failure concerns DECISION-bearing content (e.g. `approved` has
        the wrong type, `modifications[0].new_value` isn't numeric) rather
        than a narrative field — a repair call cannot fix that without
        re-deciding, so the caller should fail closed immediately instead
        of spending a call the fix-closed comparison would reject anyway.
        """
        try:
            errors = error.errors()
        except AttributeError:
            return False
        return any(
            err.get("loc", (None,))[:1] and err["loc"][0] in field_names
            for err in errors
        )

    def _execute(
        self,
        user_message: str,
        *,
        retry_kind: str | None = None,
        optional_retry: bool = False,
        single_provider_attempt: bool = False,
    ) -> AgentResult:
        """The retry / cross-provider-failover / cost / parse loop, decoupled
        from build_user_message so a stored historical `input_message` can be
        replayed through the CURRENT prompt + model without rebuilding context
        (see src/replay.py / scripts/replay_decision.py). `run()` = build +
        `_execute`; behavior is identical to the pre-extraction loop."""
        logger.info("Agent %s running with model %s", self.name, self.model)
        logger.info("Agent %s input:\n%s", self.name, user_message)

        max_retries = 1 if single_provider_attempt else _max_retries()
        deadline_s = _retry_deadline_s()
        loop_start = time.monotonic()
        finish_reason: str | None = None
        # What the provider says it actually charged, when it says so at all
        # (OpenRouter only). None everywhere else, and the pinned-rate
        # estimate below stands.
        reported_cost: float | None = None
        primary_error: Exception | None = None
        # Captured once, before the retry loop, so they reflect what THIS call
        # was CONFIGURED to use regardless of how the loop below resolves —
        # never mutated by retries/failover (see actual_provider below, which
        # is derived from actual_model AFTER the loop and can legitimately
        # differ on fallback).
        requested_model = self.model
        requested_provider = self._provider
        prompt_version = hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()[:12]
        reservation = None
        provider_requests = 0
        attempt_errors: list[BaseException] = []
        governed_estimate = 0
        governed_provider: str | None = None

        def _mark_circuit_unavailable(exc: BaseException) -> dict:
            marker = getattr(self._cost_circuit, "mark_unavailable", None)
            if callable(marker):
                try:
                    return marker(
                        exc,
                        run_id=getattr(reservation, "run_id", None),
                        mode=getattr(reservation, "mode", None),
                        agent_name=self.name,
                        attempts=provider_requests,
                    )
                except Exception as marker_exc:  # the fallback alert must not leak
                    logger.critical(
                        "Cost-circuit failure marker also failed: %s",
                        marker_exc, exc_info=True,
                    )
            sentinel = UnavailableLLMCostCircuit(
                exc, notifier=getattr(self._cost_circuit, "notifier", None),
            )
            return sentinel.activate_session(
                getattr(reservation, "run_id", "unscoped"),
                getattr(reservation, "mode", "unknown"),
            )

        def _record_attempt_failure(exc: BaseException) -> None:
            """Keep every attempt's failure, not just the one re-raised.

            A logical call can make several provider attempts against
            DIFFERENT providers, and only the primary's error survives to the
            caller — `run()` re-raises `primary_error` and discards whatever
            the failover hit. The cost circuit then judges whether the call
            provably cost nothing from that single exception, so a failover
            rejected with 401 (billed nothing, by definition) was invisible to
            it and the whole call was charged its conservative reserve at the
            FAILOVER model's price. On 2026-08-31 that put $0.62 on the ledger
            for two refusals and a missing credential that together cost $0,
            and the unexplained spend then latched the desk.
            """
            attempt_errors.append(exc)

        def _safe_fail_reservation(exc: BaseException) -> None:
            if self._cost_circuit is None or reservation is None:
                return
            if exc not in attempt_errors:
                attempt_errors.append(exc)
            try:
                self._cost_circuit.fail_call(
                    reservation, exc, attempt_errors=list(attempt_errors),
                )
            except Exception as accounting_exc:
                logger.critical(
                    "Cost-circuit failure accounting failed closed: %s",
                    accounting_exc, exc_info=True,
                )
                _mark_circuit_unavailable(accounting_exc)

        def _govern(model: str, *, is_failover: bool = False,
                    is_tertiary: bool = False) -> None:
            """Pace this request so the desk never floods a provider.

            Deliberately placed AFTER the cost circuit has authorized the
            attempt and immediately before the request leaves: money is
            checked first, then speed, and there is exactly one path through
            here — every transport and the failover all authorize through
            this closure, so nothing can send without being counted.

            `is_failover` is threaded explicitly (not sniffed from `model`)
            so a failover call is always charged to the FALLBACK provider's
            governor, never the primary's — see _governor_domain_for.
            """
            nonlocal governed_estimate, governed_provider
            governed_provider = _governor_domain_for(
                model, self, is_failover=is_failover, is_tertiary=is_tertiary,
            )
            governor = _TOKEN_GOVERNORS.get(governed_provider)
            if governor is None:
                governed_estimate = 0
                return
            chars = len(self.system_prompt) + len(user_message)
            governed_estimate = int(chars / _GOVERNOR_CHARS_PER_TOKEN) + self.max_tokens
            governor.charge(governed_estimate)

        def _authorize(model: str, *, is_failover: bool = False,
                       is_tertiary: bool = False) -> None:
            nonlocal provider_requests
            if self._cost_circuit is None:
                _govern(model, is_failover=is_failover, is_tertiary=is_tertiary)
                provider_requests += 1
                return
            try:
                self._cost_circuit.before_provider_attempt(reservation, model=model)
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit authorization failed",
                    state,
                ) from exc
            _govern(model, is_failover=is_failover, is_tertiary=is_tertiary)
            provider_requests += 1

        def _authorize_failover(model: str) -> None:
            _authorize(model, is_failover=True)

        def _authorize_tertiary(model: str) -> None:
            # Route 3 is a failover for the cost circuit's purposes (it is an
            # extra provider attempt on a logical call) but a DIFFERENT rate
            # domain for the governor's — see _governor_domain_for.
            _authorize(model, is_failover=True, is_tertiary=True)

        if self._cost_circuit is None and not self._allow_unmetered_for_tests:
            raise PaidAnalysisSuspended(
                "mandatory paid-analysis cost circuit is not attached",
                {
                    "available": False,
                    "suspended": True,
                    "trigger_code": "cost_circuit_not_attached",
                    "agent_name": self.name,
                },
            )
        if self._cost_circuit is not None:
            try:
                reservation = self._cost_circuit.begin_call(
                    agent_name=self.name,
                    model=self.model,
                    system_prompt=self.system_prompt,
                    user_message=user_message,
                    max_output_tokens=self.max_tokens,
                    retry_kind=retry_kind,
                    optional_retry=optional_retry,
                )
            except OptionalPaidAnalysisRetrySkipped:
                raise
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit reservation failed",
                    state,
                ) from exc
        # === Half-open gate on the primary ==================================
        # Ask the process-wide breaker whether the primary is usable. Three
        # outcomes (see RouteBreaker):
        #   closed              -> normal, full retry budget on the primary.
        #   half-open, we won   -> ONE probe attempt on the primary. Its
        #                          success un-demotes the route for everybody.
        #   open / probe lost   -> skip the primary entirely, start at route 2.
        #
        # CRITICAL, cost-circuit: the skip records NO exception into
        # `attempt_errors`. A synthetic "we didn't try" marker would carry no
        # `status_code`, `_is_known_zero_cost_failure` would fail closed on
        # it, and a call the desk deliberately DIDN'T make would be booked
        # ambiguous and hard-latch the desk. The skip is an absence of an
        # attempt, and it is represented as one.
        primary_allowed = self._route_breaker.primary_available()
        probing_primary = primary_allowed and self._route_breaker.is_probing()
        if probing_primary:
            # One attempt only. A probe is a question ("is it back?"), not a
            # retry sequence; spending the full budget re-asking a route that
            # was down 5 minutes ago burns the wall-clock deadline that the
            # secondary and tertiary still have to fit inside.
            max_retries = 1
            logger.warning(
                "Agent %s: HALF-OPEN probe of demoted primary %s/%s.",
                self.name, self._provider, self.model,
            )
            in_price, out_price = _route_price(self.model)
            llm_route_journal.record(
                "probe_primary", agent_name=self.name,
                run_id=getattr(reservation, "run_id", None),
                route=f"{self._provider}/{self.model}", tier=1,
                input_usd_per_mtok=in_price, output_usd_per_mtok=out_price,
                detail="cooldown elapsed; probing whether the primary recovered",
            )
        elif not primary_allowed:
            max_retries = 0
            logger.warning(
                "Agent %s: primary %s/%s is DEMOTED (cooling down) — starting "
                "at the secondary route without attempting it.",
                self.name, self._provider, self.model,
            )
        # One-shot latch for the 402 shrink-retry below, and the ask we
        # restore afterwards so a single poor-balance call cannot silently
        # shrink every later call on this seat.
        shrunk_for_credit = False
        configured_max_tokens = self.max_tokens
        for attempt in itertools.count():
            try:
                if self._use_deepseek:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_deepseek(
                        user_message, authorize=_authorize,
                    )
                elif self._use_openrouter or self._use_google:
                    # OpenRouter and Google AI Studio's compat endpoint are
                    # both OpenAI-wire-compatible — reuse _call_openai
                    # unmodified rather than duplicating the streaming/usage/
                    # empty-content logic.
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_openai(
                        user_message, authorize=_authorize,
                    )
                elif self._use_openai:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_openai(
                        user_message, authorize=_authorize,
                    )
                else:
                    (raw_text, input_tokens, output_tokens, finish_reason,
                     reported_cost) = self._call_anthropic(
                        user_message, authorize=_authorize,
                    )
                primary_error = None
                # The primary answered. If it was demoted, this un-demotes it
                # for EVERY seat sharing the pair — the "backups are
                # temporary" half of the owner's ruling.
                if self._route_breaker.record_success():
                    in_price, out_price = _route_price(self.model)
                    logger.warning(
                        "Agent %s: primary %s/%s RESTORED — the desk is back "
                        "on its configured route.",
                        self.name, self._provider, self.model,
                    )
                    llm_route_journal.record(
                        "route_restored", agent_name=self.name,
                        run_id=getattr(reservation, "run_id", None),
                        route=f"{self._provider}/{self.model}", tier=1,
                        input_usd_per_mtok=in_price,
                        output_usd_per_mtok=out_price,
                        detail="half-open probe succeeded; breaker closed",
                    )
                break
            except PaidAnalysisSuspended as exc:
                _safe_fail_reservation(exc)
                raise
            except Exception as e:
                primary_error = e
                _record_attempt_failure(e)
                # Non-retryable (auth / bad-request / 4xx / context-length):
                # stop retrying — sleeping won't help. (Was: raise. Now we
                # break so the cross-provider failover below can still try.)
                # Credit refusal that NAMES a smaller allowance it would
                # serve: re-ask once at exactly that figure instead of
                # treating the call as dead. One shot only, and only
                # downwards. See `_affordable_max_tokens`.
                affordable = _affordable_max_tokens(e)
                if (affordable is not None and not shrunk_for_credit
                        and affordable < self.max_tokens
                        and attempt < max_retries - 1):
                    shrunk_for_credit = True
                    logger.warning(
                        "Agent %s attempt %d: provider refused for "
                        "insufficient credit but stated it can serve "
                        "%d output tokens (we asked for %d). Retrying once "
                        "at the provider's stated allowance. A cut-off "
                        "answer is still discarded unused.",
                        self.name, attempt + 1, affordable, self.max_tokens,
                    )
                    self.max_tokens = affordable
                    continue
                if not _is_retryable(e):
                    if is_payment_refusal(e):
                        # TERMINAL, on the first occurrence. The account is
                        # out of credit; only a human topping it up changes
                        # that, so this route stops here and every remaining
                        # rung on the SAME account is skipped below. Zero
                        # retries is not a tuned number — a terminal error
                        # has no retry count to pick. (The one shrink-retry
                        # above is not a retry of this request: it is a
                        # smaller request the provider itself said it would
                        # still serve, and it has already been spent or
                        # declined by the time we get here.)
                        logger.error(
                            "Agent %s attempt %d: the paid research account is "
                            "OUT OF CREDIT — the provider refused to serve the "
                            "call%s. Not retrying; topping the account up is "
                            "the only fix. %s (%s)", self.name, attempt + 1,
                            "" if affordable is not None else " and named no allowance it would serve",
                            _balance_line(), e,
                        )
                    else:
                        logger.warning(
                            "Agent %s attempt %d hit a non-retryable error: %s. "
                            "No more retries.", self.name, attempt + 1, e,
                        )
                    break
                # Attempt budget. For a CAPACITY refusal (429/5xx — the
                # provider saying "busy now, usually temporary") the bound is
                # the wall-clock deadline below, NOT this count. Measured from
                # this desk's own production log 2026-09-29/30: the tech
                # seat's Google 503 "experiencing high demand ... usually
                # temporary" burned both attempts 2 SECONDS apart inside a
                # 480s deadline, fell through to paid routes that were out of
                # credit, and the blocking seat produced nothing — while the
                # same model answered normally later in the same session. Two
                # attempts two seconds apart is not a measure of whether a
                # capacity spike has passed. The class is deliberately
                # NARROW — `is_capacity_refusal`, i.e. an explicit 429/5xx —
                # because only there is the refused attempt provably unbilled
                # and the provider itself saying to wait. Everything else (a
                # degenerate 200, a transport blip, a stream cut) keeps the
                # original count, unchanged. The growing
                # full-jitter sleep and the existing deadline bound this; no
                # new number is introduced.
                capacity_class = (is_capacity_refusal(e)
                                  and not single_provider_attempt)
                attempt_cap = (capacity_max_attempts() if capacity_class
                               else max_retries)
                if attempt >= attempt_cap - 1:
                    logger.warning("Agent %s attempt %d failed: %s. Primary exhausted.",
                                   self.name, attempt + 1, e)
                    break
                # Wall-clock deadline: the attempt budget alone doesn't bound
                # time (each attempt can burn 120-380s in the relay-524 mode),
                # so exhausting it can collide with the wrapper's 1200s kill —
                # which is where the failover below became unreachable
                # (2026-06-08/09). Past the deadline, abandon the primary NOW
                # so failover fires while the session window still has room.
                elapsed = time.monotonic() - loop_start
                if elapsed >= deadline_s:
                    logger.warning(
                        "Agent %s attempt %d failed: %s. Retry deadline %.0fs "
                        "exceeded (elapsed %.0fs) — abandoning primary, "
                        "proceeding to failover if configured.",
                        self.name, attempt + 1, e, deadline_s, elapsed,
                    )
                    break
                # Error-AWARE backoff (see error_aware_backoff_seconds): a
                # server that stated a Retry-After is obeyed as stated, a
                # capacity/transient failure gets AWS full jitter, and a
                # fatal class returns None. The None branch is defensive —
                # `_is_retryable` above has already broken out of the loop for
                # every fatal class — but the two agreeing is asserted by a
                # test rather than assumed.
                kind, _hint = classify_backoff(e)
                wait = error_aware_backoff_seconds(attempt, e)
                if wait is None:
                    logger.warning(
                        "Agent %s attempt %d: %s classified non-retryable by "
                        "the backoff taxonomy. Stopping.",
                        self.name, attempt + 1, e,
                    )
                    break
                if kind == BACKOFF_RETRY_AFTER:
                    logger.warning(
                        "Agent %s attempt %d failed: %s. Server stated "
                        "Retry-After — honouring %.1fs.",
                        self.name, attempt + 1, e, wait,
                    )
                    llm_route_journal.record(
                        "retry_after", agent_name=self.name,
                        run_id=getattr(reservation, "run_id", None),
                        route=f"{self._provider}/{self.model}", tier=1,
                        wait_s=wait, error=e,
                        detail="honoured the server's own Retry-After",
                    )
                else:
                    logger.warning(
                        "Agent %s attempt %d failed: %s. Full-jitter backoff "
                        "%.1fs...", self.name, attempt + 1, e, wait,
                    )
                time.sleep(wait)

        # Restore the configured ask: the shrunken allowance belonged to the
        # one refusal that named it, not to this seat forever.
        self.max_tokens = configured_max_tokens

        # Model that actually produced the output — primary unless a backup wins.
        actual_model = self.model
        actual_route_provider: str | None = None
        if primary_error is not None:
            # The primary failed on every attempt it was given. Demote it so
            # the NEXT call in the next few minutes skips it instead of
            # re-discovering the same outage, and so the half-open probe has
            # something to come back to. Doing this before the backups run
            # means a fan-out's later threads benefit immediately.
            cooldown = self._route_breaker.record_failure()
            in_price, out_price = _route_price(self.model)
            llm_route_journal.record(
                "route_demoted", agent_name=self.name,
                run_id=getattr(reservation, "run_id", None),
                route=f"{self._provider}/{self.model}", tier=1,
                input_usd_per_mtok=in_price, output_usd_per_mtok=out_price,
                wait_s=cooldown, error=primary_error,
                detail=(
                    f"account out of credit; demoted for {cooldown:.0f}s "
                    f"without further attempts. {_balance_line()}"
                    if is_payment_refusal(primary_error)
                    else f"primary exhausted; demoted for {cooldown:.0f}s"
                ),
            )
        if primary_error is not None or not primary_allowed:
            # === The ladder ===================================================
            # Route 2 (same MODEL, different ROAD) then route 3 (different
            # MODEL). Each is single-shot: the primary already burned the
            # retry budget, and a second full budget per backup could blow
            # the session window. Order matters — route 2 is the cheaper and
            # the less surprising answer, and the 2026-08-31 "change the road,
            # not the reasoning" ruling says to exhaust it first.
            #
            # `not primary_allowed` is the demoted path: the primary was
            # skipped, so there IS no primary_error, but the ladder must still
            # run. `_last_route_error` is what gets raised if the whole ladder
            # fails on that path, since there is no primary error to re-raise.
            failover = None
            last_route_error: Exception | None = primary_error
            # Providers that have already refused this call for lack of
            # credit. A payment refusal is an ACCOUNT-level answer, not a
            # model-level one, so every remaining rung on the same account
            # is skipped instead of being paid for out of the attempt budget
            # the circuit then has to account for. Keyed on the status code
            # (see `is_payment_refusal`), never on the provider's wording.
            refused_providers: set[str] = {
                self._provider for exc in attempt_errors
                if is_payment_refusal(exc)
            }
            # Skip route 2 when ITS provider is inside a cooldown: paying a
            # call to an account that refused one minutes ago is the same
            # waste the primary skip removes, one rung down.
            secondary_open = not self._fallback_breaker.demoted()
            if not secondary_open:
                logger.warning(
                    "Agent %s: secondary provider %s is DEMOTED — skipping "
                    "route 2 and going straight to the tertiary.",
                    self.name, self._fallback_provider,
                )
            if self._fallback_provider in refused_providers:
                logger.error(
                    "Agent %s: skipping route 2 — %s has already refused this "
                    "call because the account is out of credit, and another "
                    "attempt on the same account cannot succeed.",
                    self.name, self._fallback_provider,
                )
            if (not single_provider_attempt and self._failover_reachable
                    and secondary_open
                    and self._fallback_provider not in refused_providers):
                try:
                    failover = self._try_failover(
                        user_message, primary_error, authorize=_authorize_failover,
                        on_failure=_record_attempt_failure,
                    )
                except PaidAnalysisSuspended as exc:
                    _safe_fail_reservation(exc)
                    raise
                if failover is None:
                    self._fallback_breaker.record_failure()
                    if any(is_payment_refusal(exc) for exc in attempt_errors):
                        refused_providers.add(self._fallback_provider)
                else:
                    self._fallback_breaker.record_success()
            if failover is not None:
                actual_model = self._fallback_model
                actual_route_provider = self._fallback_provider
                in_price, out_price = _route_price(self._fallback_model)
                llm_route_journal.record(
                    "route_switch", agent_name=self.name,
                    run_id=getattr(reservation, "run_id", None),
                    route=f"{self._fallback_provider}/{self._fallback_model}",
                    from_route=f"{self._provider}/{self.model}", tier=2,
                    input_usd_per_mtok=in_price, output_usd_per_mtok=out_price,
                    error=primary_error,
                    detail="secondary route carried the call (same model, "
                           "different road)",
                )
            else:
                # Route 3. Only reached when the same model on two roads has
                # already failed — which is the saturated-MODEL case route 3
                # exists for, and precisely what happened on 2026-09-22.
                tertiary = None
                # Route 3 is the last rung; it is NOT skipped on its own
                # breaker being demoted when that breaker is shared with a
                # route already skipped, because "skip the last resort too"
                # means the desk produces nothing. A demoted tertiary is
                # still tried — the cooldown's job at this depth is to
                # inform, not to forbid.
                if self._tertiary_provider in refused_providers:
                    logger.error(
                        "Agent %s: skipping route 3 — %s is out of credit, so "
                        "the last rung cannot answer either. The account needs "
                        "topping up.", self.name, self._tertiary_provider,
                    )
                if (not single_provider_attempt and self._tertiary_reachable
                        and self._tertiary_provider not in refused_providers):
                    try:
                        tertiary = self._try_tertiary(
                            user_message, authorize=_authorize_tertiary,
                            on_failure=_record_attempt_failure,
                        )
                    except PaidAnalysisSuspended as exc:
                        _safe_fail_reservation(exc)
                        raise
                    if tertiary is None:
                        self._tertiary_breaker.record_failure()
                    else:
                        self._tertiary_breaker.record_success()
                if tertiary is not None:
                    failover = tertiary
                    actual_model = self._tertiary_model
                    actual_route_provider = self._tertiary_provider
                    in_price, out_price = _route_price(self._tertiary_model)
                    llm_route_journal.record(
                        "route_switch", agent_name=self.name,
                        run_id=getattr(reservation, "run_id", None),
                        route=f"{self._tertiary_provider}/{self._tertiary_model}",
                        from_route=f"{self._fallback_provider}/{self._fallback_model}",
                        tier=3,
                        input_usd_per_mtok=in_price,
                        output_usd_per_mtok=out_price,
                        error=primary_error,
                        detail=(
                            "tertiary route carried the call (DIFFERENT ROAD "
                            "— routes 1 and 2 shared one provider and it was "
                            "down)"
                            if self._tertiary_on_alt_road else
                            "tertiary route carried the call (DIFFERENT "
                            "model — both routes on the primary model failed)"
                        ),
                    )
                elif primary_error is None and attempt_errors:
                    # ONLY on the demoted path. When the primary DID fail,
                    # the long-standing contract is that its error is what
                    # the caller sees — the primary's failure is the one the
                    # operator needs to diagnose, and a backup's error is a
                    # consequence, not a cause. Overwriting it here was a
                    # regression this line exists to prevent.
                    last_route_error = attempt_errors[-1]
            if failover is None:
                if last_route_error is None:
                    # Demoted primary, and no backup was even reachable. Say
                    # so precisely rather than raising something misleading:
                    # nothing was attempted, so there is no provider error to
                    # report and `attempt_errors` is correctly empty — which
                    # keeps `fail_call` out of the ambiguous branch.
                    last_route_error = RuntimeError(
                        f"{self.name}: primary {self._provider}/{self.model} is "
                        "demoted and no backup route is reachable"
                    )
                _safe_fail_reservation(last_route_error)
                raise last_route_error
            (raw_text, input_tokens, output_tokens, finish_reason,
             reported_cost) = failover

        # Truncation detection: a max_tokens / length cutoff means the output
        # is incomplete, NOT a deliberate "no action". Flag + log loudly so a
        # truncated decision isn't silently collapsed into "no trades".
        # max_tokens (Anthropic) / length (OpenAI+DeepSeek) = hit the ceiling;
        # insufficient_system_resource = DeepSeek cut-off-on-200. Shared
        # constant with the empty-content guards in the _call_* paths.
        truncated = (isinstance(finish_reason, str)
                     and finish_reason.lower() in _TRUNCATION_FINISH_REASONS)
        if truncated:
            logger.warning(
                "Agent %s response was TRUNCATED (finish_reason=%s) — output is "
                "incomplete, likely hit max_tokens=%d. Treat downstream None as "
                "'cut off', not 'no signal'.",
                self.name, finish_reason, self.max_tokens,
            )

        # The governor's window must reflect what was really sent, not what
        # the pre-request estimate guessed, or the ceiling quietly means
        # something other than it says.
        if governed_provider and governed_estimate:
            governor = _TOKEN_GOVERNORS.get(governed_provider)
            if governor is not None:
                governor.reconcile(governed_estimate, input_tokens + output_tokens)

        tokens = input_tokens + output_tokens
        # Cost computation — uses src.cost_table.PRICING. Returns None
        # when model is unknown so the operator sees `$?.??` and knows
        # to update the table (vs silently understating with $0.00).
        # Also returns None when token counts are both 0 — that
        # represents "we got a response but no usage data", which the
        # operator should investigate rather than see logged as a
        # confident $0.00 entry that gets summed into daily totals.
        if input_tokens == 0 and output_tokens == 0:
            cost = None
            logger.warning(
                "Agent %s completed with zero tokens reported — flagging cost as unknown. "
                "Either the SDK didn't return usage data, or the call somehow consumed nothing. "
                "Check the LLM response and update _extract_*_usage if there's a new shape.",
                self.name,
            )
        elif reported_cost is not None:
            # The provider billed this call and told us what it charged. Prefer
            # it over the pinned per-model rate, which cannot be right for a
            # model OpenRouter serves from endpoints at different prices — it
            # would over-report on the cheap endpoint (starving the daily
            # budget of headroom it actually has) and under-report on the dear
            # one. Only the estimate is a guess; this is the invoice.
            cost = reported_cost
            estimated = estimate_cost(actual_model, input_tokens, output_tokens)
            if estimated is not None and estimated > 0:
                ratio = cost / estimated
                # A large gap is not necessarily wrong — it is exactly what a
                # half-price endpoint looks like — but it means the pinned
                # table no longer describes what this seat pays, and every
                # projection built on that table (project_session_cost.py, the
                # circuit's worst-case reservation) is off by this factor.
                if ratio < 0.5 or ratio > 1.5:
                    logger.warning(
                        "Agent %s: provider-reported cost %s differs from the "
                        "pinned-rate estimate %s (%.2fx) for %s — the reported "
                        "figure is authoritative and is what was recorded, but "
                        "the pinned rate for this model no longer reflects the "
                        "endpoint serving it.",
                        self.name, fmt_cost(cost), fmt_cost(estimated),
                        ratio, actual_model,
                    )
        else:
            cost = estimate_cost(actual_model, input_tokens, output_tokens)
        if self._cost_circuit is not None and reservation is not None:
            try:
                self._cost_circuit.complete_call(
                    reservation, cost, actual_model=actual_model,
                    # Every attempt that did NOT produce this response.
                    # Empty on a clean first-attempt success; on a retry or a
                    # successful failover it names what the earlier attempts
                    # failed with, which is the only evidence the circuit has
                    # that those attempts billed nothing (see complete_call's
                    # `failed_attempt_reserve` note -- 2026-09-02).
                    failed_attempt_errors=list(attempt_errors),
                )
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                state = _mark_circuit_unavailable(exc)
                raise PaidAnalysisSuspended(
                    "mandatory cost-circuit completion accounting failed",
                    state,
                ) from exc
        logger.info(
            "Agent %s completed | tokens in=%d out=%d total=%d | cost=%s | model=%s",
            self.name, input_tokens, output_tokens, tokens,
            fmt_cost(cost), actual_model,
        )
        logger.info("Agent %s output:\n%s", self.name, raw_text)
        # "A backup answered", which is now true on tier 2 OR tier 3, and
        # true when the primary was SKIPPED (demoted) as well as when it
        # failed — `actual_route_provider` is set by whichever ladder rung
        # actually produced the text, so it is the honest test.
        used_fallback = actual_route_provider is not None
        # A vendor/model OpenRouter id is not inferable from its text. Primary
        # success therefore uses the configured provider; only a failover
        # changes attribution, to whichever provider actually answered (never
        # a fabricated/hardcoded "anthropic" string, and no longer an
        # assumption that a failover means the SECONDARY — route 3 exists).
        actual_provider = actual_route_provider or requested_provider
        latency_s = time.monotonic() - loop_start
        return AgentResult(
            raw_text=raw_text,
            tokens_used=tokens,
            model=actual_model,
            user_message=user_message,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            finish_reason=finish_reason,
            truncated=truncated,
            requested_model=requested_model,
            requested_provider=requested_provider,
            actual_provider=actual_provider,
            used_fallback=used_fallback,
            prompt_version=prompt_version,
            latency_s=latency_s,
            provider_requests=provider_requests,
        )

    def _anthropic_call(
        self, client, model: str, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """One Anthropic messages.create against an arbitrary client+model.

        Shared by the primary path (_call_anthropic) and a cross-provider
        failover that lands on Anthropic (_try_failover, when
        `fallback_provider: anthropic` is configured) so both use the
        identical request shape + usage/finish-reason extraction. Prompt
        caching is intentionally not enabled: the breaker uses the pinned
        ordinary-input rates, so enabling vendor-specific cache write/read
        pricing would make reservations and reported cost incomparable until
        that pricing is modeled explicitly.
        """
        with _ANTHROPIC_LLM_SEMAPHORE:
            if authorize is not None:
                authorize(model)
            response = client.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        in_tok, out_tok = _extract_anthropic_usage(response, self.name)
        finish_reason = getattr(response, "stop_reason", None)
        if not isinstance(finish_reason, str):
            finish_reason = None
        if not response.content or not hasattr(response.content[0], "text"):
            if finish_reason in _TRUNCATION_FINISH_REASONS:
                # Legit truncation (whole budget burned before any text) —
                # surface as truncated '', don't retry/fail over.
                logger.warning("Anthropic returned empty content (stop_reason=%s)", finish_reason)
                return ("", in_tok, out_tok, finish_reason, None)
            raise LLMEmptyResponseError(
                f"Anthropic returned empty content (stop_reason={finish_reason})"
            )
        return (response.content[0].text, in_tok, out_tok, finish_reason, None)

    def _call_anthropic(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        return self._anthropic_call(
            self.client, self.model, user_message, authorize=authorize,
        )

    def _try_failover(self, user_message: str, primary_error: Exception, *,
                      authorize=None, on_failure=None):
        """Primary provider exhausted its retries → attempt ONE call on the
        configured fallback (provider, model). Returns the (text, in_tok,
        out_tok, finish_reason, reported_cost) tuple on success, or None on
        failure (caller re-raises the original primary error). Single-shot:
        the primary already burned its retry budget, so a second full budget
        here could blow the session window. Loud logging either way — a
        provider failover is an event the operator must see.

        Builds its own client via `_build_llm_client` (the same helper
        __init__ uses for the primary) rather than duplicating client-
        construction logic, and dispatches to the Anthropic SDK path when the
        fallback provider is Anthropic, or the shared OpenAI-wire path
        (`_openai_wire_call`) for everything else — OpenAI, DeepSeek,
        OpenRouter, or Google all speak that same chat.completions shape.
        """
        logger.error(
            "Agent %s: primary %s/%s failed after retries (%s) — failing over "
            "to %s/%s.", self.name, self._provider, self.model, primary_error,
            self._fallback_provider, self._fallback_model,
        )
        try:
            client = _build_llm_client(self._fallback_provider, self._fallback_api_key)
            if self._fallback_provider == "anthropic":
                result = self._anthropic_call(
                    client, self._fallback_model, user_message, authorize=authorize,
                )
            else:
                result = self._openai_wire_call(
                    client, self._fallback_model, self._fallback_provider,
                    user_message, authorize=authorize,
                )
            logger.warning(
                "Agent %s: FAILOVER to %s/%s SUCCEEDED (in=%d out=%d) — "
                "session continues.", self.name, self._fallback_provider,
                self._fallback_model, result[1], result[2],
            )
            return result
        except PaidAnalysisSuspended:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Agent %s: failover to %s/%s also FAILED: %s. Re-raising the "
                "original primary error.", self.name, self._fallback_provider,
                self._fallback_model, exc,
            )
            if on_failure is not None:
                on_failure(exc)
            return None

    def _try_tertiary(self, user_message: str, *, authorize=None, on_failure=None):
        """Route 3: one call on a genuinely DIFFERENT model.

        Reached only when the primary AND the secondary have both failed —
        i.e. when the same model on two different roads is unavailable, which
        is the saturated-MODEL case that route diversity alone cannot survive
        (2026-09-22: Google direct refused with "This model is currently
        experiencing high demand" 17 times and the same-model OpenRouter
        failover went down with it).

        Single-shot, same as `_try_failover`, and structured identically so
        the two cannot drift. Returns the result tuple or None.
        """
        logger.error(
            "Agent %s: primary AND secondary both failed — escalating to the "
            "TERTIARY route %s/%s (a DIFFERENT model).",
            self.name, self._tertiary_provider, self._tertiary_model,
        )
        try:
            client = _build_llm_client(self._tertiary_provider, self._tertiary_api_key)
            if self._tertiary_provider == "anthropic":
                result = self._anthropic_call(
                    client, self._tertiary_model, user_message, authorize=authorize,
                )
            else:
                result = self._openai_wire_call(
                    client, self._tertiary_model, self._tertiary_provider,
                    user_message, authorize=authorize,
                )
            logger.warning(
                "Agent %s: TERTIARY %s/%s SUCCEEDED (in=%d out=%d) — the desk "
                "continues on a different model. Treat this answer as coming "
                "from a model the seat was not measured on.",
                self.name, self._tertiary_provider, self._tertiary_model,
                result[1], result[2],
            )
            return result
        except PaidAnalysisSuspended:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Agent %s: tertiary %s/%s also FAILED: %s. Every route is down.",
                self.name, self._tertiary_provider, self._tertiary_model, exc,
            )
            if on_failure is not None:
                on_failure(exc)
            return None

    def _openai_wire_call(
        self, client, model: str, provider: str, user_message: str, *,
        provider_order: list[str] | None = None, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """One streamed chat.completions call against an arbitrary
        client+model+provider. Shared by every OpenAI-wire-compatible
        provider — OpenAI, DeepSeek's cousin OpenRouter, Google's compat
        endpoint — and by a cross-provider failover landing on any of them
        (_try_failover). `_call_openai` is a thin wrapper over this for the
        primary path.

        STREAMED on purpose. The OPENAI_BASE_URL relay sits behind
        Cloudflare, whose ~120s Proxy Read Timeout (HTTP 524) kills any call
        that sends zero bytes until the model finishes — and PM / tech /
        evening generations legitimately run 120s+, so non-streaming could
        never succeed through the relay (the 2026-06-08/09 outage: every long
        call 524'd, book froze sell-only). Streaming keeps bytes flowing so
        the proxy window never trips; _LLM_HTTP_TIMEOUT becomes a per-chunk
        read timeout, so a long *healthy* generation isn't axed either.

        The semaphore covers create + iteration: for a streamed response the
        request is in flight (and counts against a relay's per-user
        concurrency cap) until the last chunk is read. OpenRouter and Google
        are each a distinct account/rate-limit domain, so each gets its own
        semaphore rather than contending with the OpenAI relay's cap.
        """
        semaphore = {
            "openrouter": _OPENROUTER_LLM_SEMAPHORE,
            "google": _GOOGLE_LLM_SEMAPHORE,
        }.get(provider, _OPENAI_LLM_SEMAPHORE)
        # OpenRouter-only request extras. `usage.include` makes OpenRouter
        # return what it ACTUALLY charged for the call, which is the only
        # honest way to price a model served from endpoints at different
        # rates; `provider.order` expresses this seat's endpoint preference.
        # `reasoning` and `response_format` are the uniform-testing settings
        # (owner requirement, 2026-09-14): every seat gets the SAME explicit
        # thinking budget instead of each model's own undeclared default —
        # the incident this fixes is qwen3.8-flash/glm-5.3 silently spending
        # their whole max_completion_tokens on hidden reasoning and returning
        # a truncated (scored 0) answer. See
        # https://openrouter.ai/docs/use-cases/reasoning-tokens and
        # https://openrouter.ai/docs/features/structured-outputs.
        extra_body: dict = {}
        if provider == "openrouter":
            extra_body["usage"] = {"include": True}
            if provider_order:
                extra_body["provider"] = {
                    "order": list(provider_order),
                    "allow_fallbacks": True,
                }
            extra_body["reasoning"] = {"effort": self._reasoning_effort}
            if self._structured_output and self.result_model is not None:
                extra_body["response_format"] = _response_format_for(self.result_model)
        elif provider == "google":
            # Google AI Studio direct is served through Google's own
            # OpenAI-compatibility endpoint (_GOOGLE_BASE_URL, .../v1beta/
            # openai/), which documents its OWN `reasoning_effort` /
            # `response_format` support — not OpenRouter's. Per
            # https://ai.google.dev/gemini-api/docs/openai (fetched
            # 2026-09-14): `reasoning_effort` is a top-level request field
            # accepting "minimal"|"low"|"medium"|"high"|"none" (the last
            # 2.5-models-only), which the endpoint itself maps internally to
            # that model's `thinking_level`/`thinking_budget` — the same
            # documented table lists Gemini 3 / 3.1 and 2.5 families, so we
            # forward the SAME llm.reasoning_effort value used for
            # OpenRouter rather than inventing our own token/level mapping.
            # Same page's structured-output section documents `response_
            # format` accepting a schema (shown there via Pydantic/Zod
            # helpers); since this endpoint is OpenAI-wire-compatible we
            # reuse the identical OpenAI-style {"type": "json_schema", ...}
            # dict `_response_format_for` already builds for OpenRouter.
            _GOOGLE_DOCUMENTED_EFFORTS = {"minimal", "low", "medium", "high", "none"}
            if self._reasoning_effort in _GOOGLE_DOCUMENTED_EFFORTS:
                extra_body["reasoning_effort"] = self._reasoning_effort
            else:
                logger.warning(
                    "Agent %s: reasoning_effort=%r has no documented Google "
                    "AI Studio equivalent (see https://ai.google.dev/gemini-"
                    "api/docs/openai) — leaving thinking level UNSET for "
                    "this call rather than inventing one.",
                    self.name, self._reasoning_effort,
                )
            if self._structured_output and self.result_model is not None:
                extra_body["response_format"] = _response_format_for(self.result_model)
        with semaphore:
            if authorize is not None:
                authorize(model)
            stream = client.chat.completions.create(
                model=model,
                max_completion_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_message},
                ],
                stream=True,
                stream_options={"include_usage": True},
                **({"extra_body": extra_body} if extra_body else {}),
            )
            parts: list[str] = []
            finish_reason: str | None = None
            usage = None
            # The id OpenRouter's /generation lookup is keyed on (see
            # _recover_openrouter_generation below) — every chunk on a real
            # stream repeats the same generation id, so keep the last
            # non-empty one seen. isinstance-guarded (not just truthy) so a
            # test double's auto-attribute MagicMock — truthy but not a str —
            # can never be mistaken for a real id.
            generation_id: str | None = None
            for chunk in stream:
                chunk_id = getattr(chunk, "id", None)
                if isinstance(chunk_id, str) and chunk_id:
                    generation_id = chunk_id
                # A mid-stream provider error. OpenRouter cannot change the
                # HTTP status once the first byte is out, so it reports the
                # failure as a top-level `error` object on a chunk (with
                # finish_reason "error"). Checked BEFORE content/usage: this
                # chunk carries no usable output, and letting it fall through
                # produced an empty body whose exception had no status_code —
                # which the cost circuit then charged in full. See
                # LLMStreamErrorChunk. Read defensively via model_extra too,
                # since the OpenAI SDK models the field as an extra.
                chunk_error = getattr(chunk, "error", None)
                if chunk_error is None:
                    extra = getattr(chunk, "model_extra", None) or {}
                    chunk_error = extra.get("error") if isinstance(extra, dict) else None
                if chunk_error:
                    if isinstance(chunk_error, dict):
                        code = chunk_error.get("code")
                        message = chunk_error.get("message") or ""
                        meta = chunk_error.get("metadata") or {}
                        etype = meta.get("error_type") if isinstance(meta, dict) else None
                    else:
                        code = getattr(chunk_error, "code", None)
                        message = getattr(chunk_error, "message", "") or ""
                        meta = getattr(chunk_error, "metadata", None)
                        etype = getattr(meta, "error_type", None) if meta else None
                    # `code` is documented as the HTTP status the response
                    # would have carried had the headers not already gone.
                    status = code if isinstance(code, int) and not isinstance(code, bool) else None
                    logger.warning(
                        "Agent %s: provider error INSIDE the stream "
                        "(code=%s type=%s): %s — surfacing with its status so "
                        "the cost circuit can judge whether it billed.",
                        self.name, status, etype, message,
                    )
                    raise LLMStreamErrorChunk(
                        f"provider error mid-stream (code={status}, "
                        f"type={etype}): {message}",
                        status_code=status, error_type=etype,
                    )
                # include_usage delivers usage on a final extra chunk whose
                # choices list is empty.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                piece = getattr(delta, "content", None) if delta is not None else None
                if piece:
                    parts.append(piece)
                fr = getattr(choice, "finish_reason", None)
                if isinstance(fr, str):
                    finish_reason = fr
        content = "".join(parts)
        if finish_reason is None:
            # Stream ended without a finish_reason = connection cut
            # mid-generation (relay drop, no error frame). Partial text must
            # NOT be returned as success — a half-emitted PM decision parses
            # like 'no trades'. Raise retryable instead.
            raise LLMStreamInterruptedError(
                f"OpenAI-wire stream ended without finish_reason after {len(content)} "
                "chars — connection cut mid-generation; partial output discarded"
            )
        if not content and finish_reason not in _TRUNCATION_FINISH_REASONS:
            # Degenerate 200 (empty body / refusal / stripped relay response):
            # entering the retry → failover machinery beats masquerading as a
            # clean no-signal. Truncation-family reasons are exempt — an empty
            # body there is a legit ceiling hit, flagged via truncated=True.
            raise LLMEmptyResponseError(
                f"OpenAI-wire call returned empty content (finish_reason={finish_reason})"
            )
        if usage is not None:
            in_tok = _coerce_token_count(getattr(usage, "prompt_tokens", 0))
            out_tok = _coerce_token_count(getattr(usage, "completion_tokens", 0))
            reported_cost = _reported_cost_usd(usage, self.name)
        else:
            # Character heuristics are not billing telemetry. The default,
            # UNLESS the OpenRouter recovery below finds a real figure, is
            # still 0/0/unknown — deliberately routing this success through
            # the breaker's unknown-actual-cost path: retain the full
            # reservation and latch instead of releasing it against a soft
            # estimate.
            in_tok, out_tok, reported_cost = 0, 0, None
            recovered = None
            if self._use_openrouter and generation_id:
                recovered = _recover_openrouter_generation(
                    generation_id, self.client.api_key, self.name,
                )
            if recovered is not None:
                in_tok, out_tok, reported_cost = recovered
                logger.warning(
                    "Agent %s: stream for generation %s carried no usage "
                    "chunk, but OpenRouter's /generation endpoint recovered "
                    "the real billing after the fact — cost=%s tokens_in=%d "
                    "tokens_out=%d. This REPLACES what would otherwise have "
                    "settled as a full conservative reservation charge.",
                    self.name, generation_id, fmt_cost(reported_cost),
                    in_tok, out_tok,
                )
            else:
                # Either not OpenRouter, no generation id was ever seen on
                # the stream, or the bounded recovery gave up (its own
                # warning states which, and why) — either way the cost stays
                # unknown and this settles at the conservative
                # full-reservation charge, exactly as before this path
                # existed.
                logger.warning(
                    "OpenAI-wire stream for %s carried no usage chunk — token "
                    "counts and actual cost are unknown; paid analysis will "
                    "suspend.",
                    self.name,
                )
        return (content, in_tok, out_tok, finish_reason, reported_cost)

    def _call_openai(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """Primary-path entry point for every OpenAI-wire-compatible
        provider (OpenAI, OpenRouter, Google) — delegates to
        `_openai_wire_call` with this agent's own client/model/provider."""
        return self._openai_wire_call(
            self.client, self.model, self._provider, user_message,
            provider_order=self._provider_order, authorize=authorize,
        )

    def _deepseek_max_output(self) -> int:
        """Clamp ceiling for this DeepSeek model. DeepSeek REJECTS (does not
        clamp) a max_tokens above the model limit, so we cap client-side."""
        return _DEEPSEEK_MAX_OUTPUT.get(self.model, _DEEPSEEK_DEFAULT_CEILING)

    def _call_deepseek(
        self, user_message: str, *, authorize=None,
    ) -> tuple[str, int, int, str | None, float | None]:
        """DeepSeek via the OpenAI SDK (custom base_url). Three deltas vs
        _call_openai:
          1. Sends `max_tokens` (DeepSeek ignores OpenAI's `max_completion_tokens`
             → output would silently fall back to a ~4096 default and truncate).
          2. Clamps to the per-model output ceiling (DeepSeek 400s on over-ceiling
             values instead of clamping).
          3. Reads the non-standard reasoning_content defensively. We DISCARD the
             chain-of-thought (every agent parses JSON from `content`), but log its
             presence so an empty-content / full-CoT truncation is visible rather
             than looking like a clean "no signal".
        Usage is OpenAI-shaped (prompt_tokens / completion_tokens) → reuse
        _extract_openai_usage.
        """
        if authorize is not None:
            authorize(self.model)
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=min(self.max_tokens, self._deepseek_max_output()),
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)
        if not isinstance(finish_reason, str):
            finish_reason = None
        if not content:
            reasoning = getattr(choice.message, "reasoning_content", None)
            if finish_reason not in _TRUNCATION_FINISH_REASONS:
                # Same guard as _call_openai: a degenerate 200 must enter the
                # retry/failover machinery, not pass as a clean no-signal.
                raise LLMEmptyResponseError(
                    f"DeepSeek returned empty content (finish_reason={finish_reason}, "
                    f"reasoning_content present={bool(reasoning)})"
                )
            # Truncation-family: reasoner burned the whole budget on CoT —
            # legit empty, surfaced via truncated=True downstream.
            logger.warning(
                "DeepSeek returned empty content (finish_reason=%s, reasoning_content present=%s)",
                finish_reason, bool(reasoning),
            )
        in_tok, out_tok = _extract_openai_usage(response, self.name)
        return (content, in_tok, out_tok, finish_reason, None)


# === Provider-specific usage extraction ===
# Both helpers (a) handle the rare case where `response.usage` is missing
# (some SDK error paths), (b) emit a WARNING when usage data is absent
# so the operator notices instead of silently logging $0 cost, and
# (c) for Anthropic, fold in the cache_creation / cache_read token
# fields. Currently we don't use prompt caching, so cache_* fields are
# always 0 and the sum equals input_tokens. If caching is ever enabled,
# this layer would need a corresponding rate adjustment in cost_table
# (cache writes = 1.25x input rate, cache reads = 0.1x) — until then
# the simple sum is harmless and forward-compatible.

def _coerce_token_count(value) -> int:
    """Return value as int iff it really IS an int (numpy.int64 subclasses
    int, so those work too). Anything else — None, MagicMock auto-attrs,
    a stray dict, a string — coerces to 0.

    This is defensive against two cases that have actually shown up:
      (1) tests using ``MagicMock`` without an explicit spec — attribute
          access auto-creates a child MagicMock whose ``__int__`` returns
          1, which would silently add +1 to every uncovered token field
          (caught by the R7 self-audit: existing tests started failing
          with 'assert 2502 == 2500' after we began summing the cache
          fields, because the cache fields weren't set in the mocks).
      (2) future SDK changes that turn a numeric field into a string
          or object — better to under-count than crash, since the
          run() layer flags 0+0 tokens as cost=unknown anyway.
    """
    # bool is a subclass of int but we never want to treat True/False as
    # token counts of 1/0.
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _reported_cost_usd(usage, agent_name: str) -> float | None:
    """The USD OpenRouter says it actually charged for a call, or None.

    Present only when the request asked for it (`usage: {include: true}`,
    OpenRouter-only) — every other provider returns None here and keeps the
    pinned-rate estimate. It matters because OpenRouter serves one model id
    from endpoints at different prices (`openai/flex` at half `openai`'s rate
    for the same `gpt-5.5` weights), so a table keyed on the model id alone
    cannot price the call: it is right for one endpoint and wrong for the
    other. Under-reporting is the dangerous direction — the daily cost
    circuit spends against these numbers — so anything that is not a finite,
    non-negative real number degrades to None and the estimate stands.

    `bool` is excluded for the same reason as in _coerce_token_count: it is a
    subclass of int and True would silently price a call at $1.
    """
    if usage is None:
        return None
    value = getattr(usage, "cost", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "Agent %s: OpenRouter reported an unusable cost (%r) — falling "
            "back to the pinned-rate estimate.", agent_name, value,
        )
        return None
    return float(value)


# Bounded retry budget for the post-hoc OpenRouter generation lookup below.
# Measured live 2026-08-31: querying a generation id immediately after its
# stream closed 404'd twice in a row, and the SAME id then resolved cleanly
# minutes later — OpenRouter's billing pipeline is asynchronous, not
# instant. Seconds of retrying here will therefore rarely out-wait a genuine
# not-yet-indexed 404; the budget exists to absorb transient blips (a slow
# DNS lookup, a dropped packet, an id that happens to land quickly) without
# meaningfully delaying a live trading session. 3 attempts x 1.0s timeout +
# 2 sleeps x 1.0s between them bounds the worst case (every attempt times
# out) at 5s, comfortably under the ~6s ceiling — well short of blocking the
# session, and if it fails the call still settles via the existing
# conservative fallback exactly as it did before this path existed.
_OPENROUTER_GENERATION_LOOKUP_ATTEMPTS = 3
_OPENROUTER_GENERATION_LOOKUP_TIMEOUT_S = 1.0
_OPENROUTER_GENERATION_LOOKUP_SLEEP_S = 1.0
_OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"


def _recover_openrouter_generation(
    generation_id: str, api_key: str, agent_name: str,
) -> tuple[int, int, float] | None:
    """Best-effort recovery of the real cost/tokens for a streamed
    OpenRouter call whose stream carried no usage chunk (e.g. a relay that
    strips stream_options include_usage).

    OpenRouter retains the true billed cost for every generation — streamed
    calls included — behind GET /generation?id=..., but see the module
    comment above _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS: the record is not
    immediately queryable, so this makes a small bounded number of short
    attempts and gives up rather than blocking a live session on an
    endpoint that may genuinely take minutes to catch up.

    Returns (tokens_prompt, tokens_completion, total_cost_usd) on success.
    Returns None on ANY failure — 404, timeout, network error, malformed
    body, non-finite/negative numbers — so the caller falls back to today's
    unknown-cost behavior. NEVER raises; this must never make a call worse
    than it already was.
    """
    import requests  # lazy import — same pattern as src/cost_table.py

    last_reason = "no attempts made"
    for attempt in range(_OPENROUTER_GENERATION_LOOKUP_ATTEMPTS):
        try:
            resp = requests.get(
                _OPENROUTER_GENERATION_URL,
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_OPENROUTER_GENERATION_LOOKUP_TIMEOUT_S,
            )
            if resp.status_code == 404:
                last_reason = "404 (generation not yet indexed)"
            else:
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    last_reason = f"malformed response body: {body!r}"
                else:
                    cost = data.get("total_cost")
                    tok_in = data.get("tokens_prompt")
                    tok_out = data.get("tokens_completion")
                    cost_ok = (
                        isinstance(cost, (int, float)) and not isinstance(cost, bool)
                        and math.isfinite(cost) and cost >= 0
                    )
                    tok_in_ok = (
                        isinstance(tok_in, int) and not isinstance(tok_in, bool)
                        and tok_in >= 0
                    )
                    tok_out_ok = (
                        isinstance(tok_out, int) and not isinstance(tok_out, bool)
                        and tok_out >= 0
                    )
                    if cost_ok and tok_in_ok and tok_out_ok:
                        return (tok_in, tok_out, float(cost))
                    last_reason = f"unusable fields in response: {data!r}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        if attempt < _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS - 1:
            time.sleep(_OPENROUTER_GENERATION_LOOKUP_SLEEP_S)
    logger.warning(
        "Agent %s: OpenRouter generation lookup for %s did not recover "
        "billing after %d attempt(s) — %s. Cost stays unknown; this call "
        "will settle at the full conservative reservation.",
        agent_name, generation_id, _OPENROUTER_GENERATION_LOOKUP_ATTEMPTS,
        last_reason,
    )
    return None


def _extract_anthropic_usage(response, agent_name: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.warning(
            "Anthropic response for %s missing usage object — cost will be flagged as unknown",
            agent_name,
        )
        return (0, 0)
    in_tok = _coerce_token_count(getattr(usage, "input_tokens", 0))
    cache_create = _coerce_token_count(getattr(usage, "cache_creation_input_tokens", 0))
    cache_read = _coerce_token_count(getattr(usage, "cache_read_input_tokens", 0))
    out_tok = _coerce_token_count(getattr(usage, "output_tokens", 0))
    # Sum across cache fields so token COUNT is correct even with caching.
    # Cost rates will need a separate refactor if caching is enabled
    # (cache write = 1.25x input rate, cache read = 0.1x).
    return (in_tok + cache_create + cache_read, out_tok)


def _extract_openai_usage(response, agent_name: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.warning(
            "OpenAI response for %s missing usage object — cost will be flagged as unknown",
            agent_name,
        )
        return (0, 0)
    prompt_tokens = _coerce_token_count(getattr(usage, "prompt_tokens", 0))

    # Cache accounting, measured rather than assumed (2026-08-31). Splitting a
    # batch into more, smaller requests repeats the system prompt more often,
    # and whether that costs anything depends entirely on whether the provider
    # is serving it from cache. On this seat's route a cached prompt token is
    # billed at $0.01/M against $0.10/M — a 10x discount that decides the
    # trade-off outright. Nobody knew which way it went because nothing ever
    # read the field. Reported, not acted on: this only ever logs.
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = _coerce_token_count(getattr(details, "cached_tokens", 0))
    if cached:
        logger.info(
            "Agent %s: %d of %d prompt tokens served from the provider's cache "
            "(%.0f%%) — repeated system prompts are being discounted.",
            agent_name, cached, prompt_tokens,
            (cached / prompt_tokens * 100) if prompt_tokens else 0.0,
        )
    return (
        prompt_tokens,
        _coerce_token_count(getattr(usage, "completion_tokens", 0)),
    )


# --- Patch mirroring. The ONE write-through ``__setattr__`` pushes any
# assignment made on this module (tests patch ``src.agents.base.<name>``) into
# every moved module that already holds that name, so moved code calls the
# patched object. No module ``__getattr__``: every moved name is imported
# explicitly above, and no moved name is rebound through ``global``.
import sys as _sys
import types as _types

_MOVED_MODULES = (
    "src.agents.llm_schema",
    "src.agents.llm_providers",
    "src.agents.llm_retry",
    "src.agents.llm_route_breaker",
    "src.agents.llm_tertiary_route",
    "src.agents.llm_concurrency",
    "src.agents.llm_attempts",
)


class _MirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module_path in _MOVED_MODULES:
            moved = _sys.modules.get(module_path)
            if moved is not None and name in vars(moved):
                setattr(moved, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module_path in _MOVED_MODULES:
            moved = _sys.modules.get(module_path)
            if moved is not None and name in vars(moved):
                delattr(moved, name)


_sys.modules[__name__].__class__ = _MirroringModule
