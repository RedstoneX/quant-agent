import logging
import os
from pathlib import Path

from src.agents.base import BaseAgent, AgentResult
from src.cost_circuit import OptionalPaidAnalysisRetrySkipped, PaidAnalysisSuspended
from src.data.context import compute_market_context, format_context_block
from src.data.levels import find_structural_levels, format_levels_block
from src.models import TechAnalysisResult, parse_telemetry
from src.token_budget import pack_to_budget, size_model_for_agent

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "tech_analyst.md"

# OHLCV bars attached per symbol in the user message. Enough for swing pivots
# and micro-structure, not so many that context balloons on a 30-symbol batch.
# Recent bars shown verbatim, for immediate context only. Structural levels no
# longer come from this window — they are computed in Python over the full
# history (see src/data/levels.py), so this stays small on purpose.
_BARS_PER_SYMBOL = 40

# Ceiling on ONE request's predicted tokens. Chosen from measurement, not
# taste: at 45k a 53-symbol universe packs into 5 requests with a ~44k peak
# and ~214k total, against today's 3 requests at a ~135k peak and ~290k
# total — both peak and total fall. Raising it to 60k saves 2% of tokens for
# a 33% higher peak; 100k saves 4% for a peak nearly three times larger. The
# curve knees here. Override with QUANT_AGENT_TECH_REQUEST_TOKENS.
_REQUEST_TOKEN_BUDGET = int(os.environ.get("QUANT_AGENT_TECH_REQUEST_TOKENS", "45000"))


def _px(value) -> str:
    """Render a price without the float64 conversion noise.

    Bars arrive as float32 from the data provider and are upcast to float64
    for arithmetic, so Python's default repr prints the upcast artefact in
    full: `O=14.920000076293945` for a stock that traded at $14.92. Every
    digit past the sixth significant one is conversion noise that was never
    price data, and it was being sent for every bar of every symbol, three
    times a session.

    Measured on the 2026-08-31 morning prompt: raw bars were 72% of the
    Technical Analyst's payload (215,617 of 296,007 characters), and this
    formatting is roughly half of that — the single largest thing the desk
    sends, and it carries no information.

    `.6g` keeps six significant digits, which is float32's own precision, so
    nothing real is lost: the source could not distinguish finer values in
    the first place. Falls back to a fixed format if a value is small or
    large enough to go scientific, and passes non-numerics through untouched
    so a missing field still renders as whatever it was.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    out = f"{value:.6g}"
    if "e" in out or "E" in out:
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return out

# Auto-chunk the batch when a single LLM call would carry too many symbols.
# 25 picked so chunks stay comfortably within typical LLM context.
#
# The "~300 input tokens per symbol (20 bars + indicators)" this was once
# calibrated against stopped being true and nobody re-derived it: the window
# grew to 40 bars, per-symbol context (prior rating, valuation, structural
# levels, relative strength, intraday block) was added on top, and the
# measured figure on 2026-08-31 was ~1,396 tokens per symbol — 4.7x the
# assumption. Context length was never the binding constraint anyway (the
# model takes 1M), but the provider's rate limit is: see the token-rate
# governor in src/agents/base.py, which now bounds what a batch may send per
# minute regardless of how this number drifts.
_MAX_SYMBOLS_PER_CALL = 30
_CHUNK_SIZE = 25

# 2026-08-19 Tech batch-response symbol-loss fix: bounded re-ask for
# symbols a chunk's first response dropped (non-JSON response, LLM
# returned fewer rows than submitted, or a row failed schema validation).
# Small and fixed — this is a recovery pass for a parsing/response
# hiccup, not a retry-until-success loop; a symbol still missing after
# this many extra attempts gets an explicit failed outcome instead.
_MAX_MISSING_RETRIES = 1


def _merge_agent_results(first: AgentResult, second: AgentResult) -> AgentResult:
    """Combine two sequential LLM-call results into one for cost/telemetry
    accounting — same merge semantics `analyze_batch` already uses to
    stitch its chunk-loop AgentResults into a single reported call, reused
    here so a `_analyze_chunk` retry's tokens/cost/latency are never
    silently dropped from what gets logged and billed against."""
    merged_cost: float | None
    if first.cost_usd is None or second.cost_usd is None:
        merged_cost = None
    else:
        merged_cost = first.cost_usd + second.cost_usd
    return AgentResult(
        raw_text=f"{first.raw_text}\n\n--- retry ---\n{second.raw_text}",
        tokens_used=first.tokens_used + second.tokens_used,
        model=second.model,
        user_message=f"{first.user_message}\n\n--- retry ---\n{second.user_message}",
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cost_usd=merged_cost,
        finish_reason=second.finish_reason,
        truncated=bool(first.truncated or second.truncated),
        requested_model=first.requested_model,
        requested_provider=second.requested_provider or first.requested_provider,
        actual_provider=second.actual_provider,
        used_fallback=bool(first.used_fallback or second.used_fallback),
        prompt_version=second.prompt_version or first.prompt_version,
        latency_s=first.latency_s + second.latency_s,
        provider_requests=first.provider_requests + second.provider_requests,
    )


class TechAnalystAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "tech_analyst"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a technical analyst. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        symbols_data: list[dict] = kwargs.get("symbols_data", []) or []
        prior_ratings: dict[str, dict] = kwargs.get("prior_ratings") or {}
        valuations: dict[str, dict] = kwargs.get("valuations") or {}
        # Yesterday's macro regime — used as a sanity checker, NOT to
        # override TA's technical call. Pipeline passes macro_store's
        # last_state (1-day stale typically). Regime very rarely flips
        # overnight, so this is a cheap additional context.
        prior_macro_regime: str | None = kwargs.get("prior_macro_regime")
        prior_macro_outlook: str | None = kwargs.get("prior_macro_outlook")
        # Current-session (TODAY, still forming) facts per symbol, from the
        # intraday scan's snapshot call. Rendered as its own clearly-labelled
        # INCOMPLETE block — never merged into the completed-daily-bar
        # series, and never used to overwrite the last completed close
        # (2026-08-19: the intraday scan detected candidates on live prices
        # but then handed Tech only bars ending at yesterday's close, so the
        # very move that triggered the scan was invisible to the analyst).
        intraday_context: dict[str, dict] = kwargs.get("intraday_context") or {}

        # How many days ago did the cached rating first appear?
        from datetime import date as _date
        from src.util.time import et_today
        today = et_today()

        def _prior_line(symbol: str) -> str:
            p = prior_ratings.get(symbol)
            if not p:
                return ""
            try:
                first = _date.fromisoformat(p.get("first_seen_date", ""))
                age = max(0, (today - first).days)
                age_str = f"{age}d ago" if age > 0 else "today (new)"
            except (ValueError, TypeError):
                age_str = "unknown age"
            entry = p.get("entry_price")
            stop = p.get("stop_loss")
            target = p.get("reference_target")
            prices = f"entry {entry} / stop {stop} / target {target}" if entry else "no prior prices"
            return (
                f"\nPrior rating (context): {p.get('rating', '?')} "
                f"({p.get('conviction', '?')}) | first seen {age_str} | {prices}"
            )

        def _valuation_line(symbol: str) -> str:
            v = valuations.get(symbol)
            if not v:
                return ""
            t = v.get("trailing_pe")
            f = v.get("forward_pe")
            ps = v.get("ps_ratio")
            # All three missing (typical for ETFs) → skip the line entirely.
            if t is None and f is None and ps is None:
                return ""
            return (
                f"\nValuation: trailing PE {t} | forward PE {f} | P/S {ps}"
            )

        def _intraday_block(symbol: str, last_completed_close) -> str:
            ic = intraday_context.get(symbol)
            if not ic:
                return ""
            last = ic.get("last_price")
            prev = ic.get("prev_close")
            if not isinstance(last, (int, float)) or last <= 0:
                return ""
            # Move is measured against the prior COMPLETED close, which is
            # what "today's move" means; fall back to the last bar in the
            # series when the snapshot didn't carry one.
            base = prev if isinstance(prev, (int, float)) and prev > 0 else None
            if base is None and isinstance(last_completed_close, (int, float)):
                base = last_completed_close if last_completed_close > 0 else None
            move_str = "n/a"
            if base:
                move_str = f"{(last - base) / base * 100:+.2f}% vs prior close ${base:,.2f}"

            def _fmt(key, prefix="$"):
                v = ic.get(key)
                if not isinstance(v, (int, float)) or v <= 0:
                    return "n/a"
                return f"{prefix}{v:,.2f}" if prefix else f"{v:,.0f}"

            return (
                f"\n⚠️ CURRENT SESSION (TODAY, INCOMPLETE — this trading day has "
                f"NOT closed; these are live intraday figures, NOT a finished "
                f"daily bar and NOT part of the completed series above):"
                f"\n  Last trade: ${last:,.2f} ({move_str})"
                f"\n  Session so far: O={_fmt('session_open')} "
                f"H={_fmt('session_high')} L={_fmt('session_low')} "
                f"V={_fmt('session_volume', prefix='')} (partial-day volume)"
                f"\n  The indicators above are computed from COMPLETED daily "
                f"bars only and therefore do NOT yet reflect this move. Judge "
                f"the setup on today's live price action against those levels, "
                f"and say so explicitly in your reasoning_chain."
            )

        # Relative strength needs something to be relative to. The index ETFs
        # are already in the universe, so the benchmark comes free from the
        # batch rather than requiring another fetch. A stock rising less than
        # its index is weak however green the candle looks — the analyst was
        # previously blind to this entirely.
        # Chosen over the WHOLE batch, not this chunk. The index ETFs sit at
        # the top of the universe, so with fixed 25-symbol chunks only the
        # first chunk ever contained SPY and every symbol after it silently
        # lost its relative-strength context — a stock rising less than its
        # index looked fine. Worse, WHICH symbols kept the benchmark depended
        # on the chunk size, so changing how batches are split would quietly
        # change the analysis. Passing the full batch's bars makes each
        # symbol's section identical however the batch is divided, which is
        # both correct and what lets chunks be packed to a size budget.
        bars_by_symbol = dict(kwargs.get("benchmark_pool") or {})
        for i in symbols_data:
            bars_by_symbol.setdefault(i["symbol"], i["bars"])
        benchmark_symbol = next(
            (s for s in ("SPY", "QQQ", "IWM") if bars_by_symbol.get(s)), None
        )
        benchmark_bars = bars_by_symbol.get(benchmark_symbol) if benchmark_symbol else None
        days_to_earnings: dict[str, int] = kwargs.get("days_to_earnings") or {}

        sections = []
        for item in symbols_data:
            symbol = item["symbol"]
            bars = item["bars"]
            indicators = item["indicators"]
            recent_bars = bars[-_BARS_PER_SYMBOL:] if len(bars) > _BARS_PER_SYMBOL else bars
            bars_text = "\n".join(
                f"  {b.date}: O={_px(b.open)} H={_px(b.high)} "
                f"L={_px(b.low)} C={_px(b.close)} V={_px(b.volume)}"
                for b in recent_bars
            )
            last_close = recent_bars[-1].close if recent_bars else "N/A"
            # Structural levels are computed in Python from the FULL history,
            # not from the recent window above. Finding where price repeatedly
            # stopped is arithmetic; asking a model to spot it in a wall of
            # OHLC rows is both unreliable and unnecessary. Before this, the
            # analyst saw 20 bars and could only cite moving averages and the
            # 20-day range as "levels" — which is why the structural stops and
            # targets the exit system depends on were effectively absent.
            supports, resistances = find_structural_levels(bars)
            levels_text = format_levels_block(
                supports,
                resistances,
                last_close if isinstance(last_close, (int, float)) else 0.0,
            )
            context_text = format_context_block(
                compute_market_context(
                    bars,
                    benchmark_bars=benchmark_bars if symbol != benchmark_symbol else None,
                    benchmark_symbol=benchmark_symbol if symbol != benchmark_symbol else None,
                ),
                days_to_earnings=days_to_earnings.get(symbol),
            )
            sections.append(f"""### {symbol}{_prior_line(symbol)}{_valuation_line(symbol)}
{context_text}
{levels_text}
Price (last {len(recent_bars)} COMPLETED daily bars):
{bars_text}
Indicators: MA20={_px(indicators.ma_20)} MA50={_px(indicators.ma_50)} MA200={_px(indicators.ma_200)} | RSI={_px(indicators.rsi_14)} | MACD={_px(indicators.macd)}/{_px(indicators.macd_signal)}/{_px(indicators.macd_hist)} | BB={_px(indicators.bb_lower)}/{_px(indicators.bb_middle)}/{_px(indicators.bb_upper)} | ATR={_px(indicators.atr_14)} | Vol%={_px(indicators.volume_change_pct)}
Last completed close: {_px(last_close)}{_intraday_block(symbol, last_close)}""")

        macro_context = ""
        if prior_macro_regime:
            macro_context = (
                f"\n## Macro Context (as of previous session — sanity-check only)\n"
                f"Regime: {prior_macro_regime}"
                + (f" | Equity outlook: {prior_macro_outlook}" if prior_macro_outlook else "")
                + "\n\nThis is NOT an override of your technical call. Use it to flag "
                "divergence in support_resistance step: e.g., 'macro is risk-off but "
                "price broke out — watch for a short-squeeze then fade back to trend'. "
                "Your rating stays driven by the chart; the macro flag is a cross-check "
                "surfaced to PM and RM.\n"
            )

        return (
            "Analyze the following symbols. For EACH symbol, walk through the 5-step "
            "reasoning_chain and respect the ATR-based stop discipline in the prompt."
            + macro_context
            + "\n\n"
            + "\n\n".join(sections)
            + "\n\nRespond with a JSON array — one object per symbol, in any order."
        )

    def analyze_batch(
        self,
        symbols_data: list[dict],
        prior_ratings: dict[str, dict] | None = None,
        valuations: dict[str, dict] | None = None,
        prior_macro_regime: str | None = None,
        prior_macro_outlook: str | None = None,
        intraday_context: dict[str, dict] | None = None,
    ) -> tuple[dict[str, TechAnalysisResult | None], "AgentResult | None"]:
        """Batch analyze multiple symbols. Auto-chunks when > 30 symbols to avoid
        context overflow on the LLM call. Returns ({symbol: result}, merged AgentResult).

        Every symbol in `symbols_data` is guaranteed to be a key in the
        returned dict (2026-08-19 Tech batch-response symbol-loss fix) —
        `None` marks a symbol that failed to resolve after the logical batch's
        bounded recovery opportunity. Callers MUST filter `None` values out
        before treating the dict's values as a list of real analyses
        (e.g. `[a for a in result.values() if a is not None]`), and should
        surface the None count rather than silently dropping it — that
        silent drop, at the pipeline_stages.py call site, was the original
        production incident this fix addresses.

        prior_ratings: optional {symbol: {rating, conviction, first_seen_date, ...}}
          from TechStore. When supplied, each symbol's user-message section prefaces
          today's data with a 'Prior rating' line so the LLM can judge continuation
          vs flip vs staleness.
        valuations: optional {symbol: {trailing_pe, forward_pe, ps_ratio}} from
          MarketDataProvider.get_valuation_metrics. Surfaced as a Valuation line
          in the prompt so the LLM can flag overvaluation in its reasoning_chain.
        prior_macro_regime / prior_macro_outlook: yesterday's regime (from
          MacroStore.last_state). Surfaced as a sanity-check input so TA can
          flag divergence in reasoning_chain.support_resistance — does NOT
          override the technical call.
        """
        if not symbols_data:
            return {}, None

        benchmark_pool = {i["symbol"]: i["bars"] for i in symbols_data}
        chunks = self._split_to_budget(
            symbols_data, prior_ratings, valuations,
            prior_macro_regime, prior_macro_outlook, intraday_context,
            benchmark_pool,
        )
        if len(chunks) <= 1:
            return self._analyze_chunk(
                symbols_data, prior_ratings, valuations,
                prior_macro_regime, prior_macro_outlook, intraday_context,
                benchmark_pool=benchmark_pool,
            )

        merged: dict[str, TechAnalysisResult | None] = {}
        result_parts: list[tuple[str, AgentResult]] = []
        # Phase 1: every primary chunk gets one chance before any repair.
        # Independent per-chunk recursion used to consume the session-wide
        # repair allowance before the later chunks were even analyzed.
        for i, chunk in enumerate(chunks, 1):
            chunk_analyses, chunk_result = self._analyze_chunk(
                chunk, prior_ratings, valuations,
                prior_macro_regime, prior_macro_outlook, intraday_context,
                _retries_left=0, benchmark_pool=benchmark_pool,
            )
            merged.update(chunk_analyses)
            if chunk_result is not None:
                result_parts.append((f"chunk {i}/{len(chunks)}", chunk_result))

        # Phase 2: one consolidated, bounded recovery for the whole logical
        # batch.  Original input order is retained; run-scoped admissions are
        # placed first by MorningResearchStage, so fresh external evidence is
        # not stranded behind the configured universe.  Anything beyond one
        # safe chunk remains explicit None and therefore cannot reach PM.
        missing_data = [
            item for item in symbols_data
            if merged.get(item.get("symbol")) is None
        ]
        if missing_data:
            recovery_data = missing_data[:_CHUNK_SIZE]
            logger.warning(
                "Tech batch incomplete across %d primary chunk(s): %d symbol(s) "
                "unresolved — one consolidated recovery for %d symbol(s); "
                "%d remain explicit failures without another paid retry",
                len(chunks), len(missing_data), len(recovery_data),
                len(missing_data) - len(recovery_data),
            )
            try:
                recovered, recovery_result = self._analyze_chunk(
                    recovery_data, prior_ratings, valuations,
                    prior_macro_regime, prior_macro_outlook, intraday_context,
                    _retries_left=0, _is_logical_retry=True,
                )
            except OptionalPaidAnalysisRetrySkipped as exc:
                recovered, recovery_result = {}, None
                logger.warning(
                    "Tech batch recovery skipped without provider I/O; shared "
                    "session retry allowance is already spent: %s", exc.trigger,
                )
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                recovered, recovery_result = {}, None
                logger.error(
                    "Tech batch optional recovery failed; retaining completed "
                    "primary analyses: %s", exc,
                )
            merged.update({
                symbol: analysis
                for symbol, analysis in recovered.items()
                if analysis is not None
            })
            if recovery_result is not None:
                result_parts.append(("missing-symbol recovery", recovery_result))

        final_missing = [
            item.get("symbol") for item in symbols_data
            if merged.get(item.get("symbol")) is None
        ]
        if final_missing:
            logger.error(
                "Tech batch: %d symbol(s) unresolved after the single shared "
                "recovery — explicit failed outcomes: %s",
                len(final_missing), final_missing,
            )

        combined_raw: list[str] = []
        combined_msg: list[str] = []
        total_tokens = 0
        total_input_tokens = 0
        total_output_tokens = 0
        # cost_usd is None until at least one chunk produces a known
        # value; if ANY chunk produces None (unknown model in cost
        # table), merged stays None — partial sum across same-model
        # chunks would just understate by the unknown chunk's cost,
        # so flag the gap.
        chunk_costs: list[float] = []
        any_unknown_cost = False
        last_model = self.model
        # Stage 1 attribution across the N-chunks-collapse-to-1-row limitation
        # (Stage 0 audit F-3, still not structurally fixable without a
        # per-chunk log row — out of Stage 1's bounded scope). requested_model/
        # requested_provider/prompt_version are constant across chunks (same
        # agent, same self.model, same system_prompt) so any chunk's value is
        # correct; used_fallback/truncated are ORed (any chunk falling back or
        # truncating makes the merged row's attribution non-clean); latency_s
        # is summed (wall time actually spent across all chunk calls).
        requested_model = self.model
        requested_provider = ""
        prompt_version = ""
        any_used_fallback = False
        any_truncated = False
        total_latency = 0.0
        total_provider_requests = 0
        last_finish_reason: str | None = None
        for label, part in result_parts:
            combined_raw.append(f"--- {label} ---\n{part.raw_text}")
            combined_msg.append(f"--- {label} ---\n{part.user_message}")
            total_tokens += part.tokens_used
            total_input_tokens += part.input_tokens
            total_output_tokens += part.output_tokens
            if part.cost_usd is None:
                any_unknown_cost = True
            else:
                chunk_costs.append(part.cost_usd)
            last_model = part.model
            requested_provider = part.requested_provider or requested_provider
            prompt_version = part.prompt_version or prompt_version
            any_used_fallback = any_used_fallback or part.used_fallback
            any_truncated = any_truncated or part.truncated
            total_latency += part.latency_s
            total_provider_requests += part.provider_requests
            last_finish_reason = part.finish_reason

        merged_cost: float | None
        if any_unknown_cost or not chunk_costs:
            merged_cost = None
        else:
            merged_cost = sum(chunk_costs)

        merged_result = AgentResult(
            raw_text="\n\n".join(combined_raw),
            tokens_used=total_tokens,
            model=last_model,
            user_message="\n\n".join(combined_msg),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=merged_cost,
            finish_reason=last_finish_reason,
            truncated=any_truncated,
            requested_model=requested_model,
            requested_provider=requested_provider,
            actual_provider=("anthropic" if any_used_fallback else requested_provider),
            used_fallback=any_used_fallback,
            prompt_version=prompt_version,
            latency_s=total_latency,
            provider_requests=total_provider_requests,
        )
        return merged, merged_result

    def _split_to_budget(
        self, symbols_data, prior_ratings, valuations,
        prior_macro_regime, prior_macro_outlook, intraday_context,
        benchmark_pool,
    ) -> list[list[dict]]:
        """Split the batch so no request exceeds the per-request token budget.

        Packs by MEASURED bytes: each symbol's section is rendered once and
        its real length used, so the only estimate anywhere is bytes-to-tokens
        — and that comes from a model fitted to this agent's own past calls
        (see src/token_budget.py), not from a guess.

        This replaces a fixed 25-symbols-per-request split that was calibrated
        against "~300 input tokens per symbol" and had drifted to ~3,600,
        producing ~135,000-token requests. A count cannot bound tokens; the
        content underneath it changes and nothing complains until a provider
        refuses the result.

        Falls back to the old fixed split on ANY problem. Sizing is an
        efficiency measure, and an efficiency measure that can stop a trading
        session is a bad trade.
        """
        if len(symbols_data) <= 1:
            return [list(symbols_data)]
        try:
            def render(item):
                return self.build_user_message(
                    symbols_data=[item],
                    prior_ratings=prior_ratings or {},
                    valuations=valuations or {},
                    prior_macro_regime=prior_macro_regime,
                    prior_macro_outlook=prior_macro_outlook,
                    intraday_context=intraday_context or {},
                    benchmark_pool=benchmark_pool or {},
                )

            # The framing that every request carries regardless of content;
            # subtracting it leaves each symbol's own contribution.
            envelope = len(self.build_user_message(
                symbols_data=[],
                prior_macro_regime=prior_macro_regime,
                prior_macro_outlook=prior_macro_outlook,
            ))
            sizes = {
                id(item): max(0, len(render(item)) - envelope)
                for item in symbols_data
            }
            model = size_model_for_agent(self)
            chunks = pack_to_budget(
                list(symbols_data),
                lambda item: sizes[id(item)],
                budget_tokens=_REQUEST_TOKEN_BUDGET,
                model=model,
                max_items=_MAX_SYMBOLS_PER_CALL,
            )
            if not chunks or sum(len(c) for c in chunks) != len(symbols_data):
                raise ValueError("packing lost or duplicated symbols")
            if len(chunks) > 1:
                peak = max(
                    model.predict(sum(sizes[id(i)] for i in c)) for c in chunks
                )
                logger.info(
                    "Tech batch: %d symbols packed into %d requests of up to "
                    "%s symbols, peak ~%d tokens against a %d budget (%s size "
                    "model: %.0f fixed + %.3f/byte).",
                    len(symbols_data), len(chunks),
                    max(len(c) for c in chunks), peak, _REQUEST_TOKEN_BUDGET,
                    "measured" if model.measured else "fallback",
                    model.fixed_tokens, model.tokens_per_byte,
                )
            return chunks
        except Exception:
            logger.warning(
                "Tech batch: could not size the request set; falling back to "
                "fixed %d-symbol chunks. Analysis is unaffected — this only "
                "changes how the batch is divided.",
                _CHUNK_SIZE, exc_info=True,
            )
            if len(symbols_data) <= _MAX_SYMBOLS_PER_CALL:
                return [list(symbols_data)]
            return [
                symbols_data[i : i + _CHUNK_SIZE]
                for i in range(0, len(symbols_data), _CHUNK_SIZE)
            ]

    def _analyze_chunk(
        self,
        symbols_data: list[dict],
        prior_ratings: dict[str, dict] | None = None,
        valuations: dict[str, dict] | None = None,
        prior_macro_regime: str | None = None,
        prior_macro_outlook: str | None = None,
        intraday_context: dict[str, dict] | None = None,
        _retries_left: int = _MAX_MISSING_RETRIES,
        _is_logical_retry: bool = False,
        benchmark_pool: dict | None = None,
    ) -> tuple[dict[str, TechAnalysisResult | None], "AgentResult | None"]:
        """Single-call variant used inside the chunking loop.

        2026-08-19 Tech batch-response symbol-loss fix: every symbol in
        `symbols_data` is guaranteed to be a key in the returned dict —
        a `TechAnalysisResult` for a successfully parsed rating (including
        `neutral`/`sell`; a considered-and-passed symbol is a terminal
        outcome too, not a loss), or `None` for a symbol that could not be
        resolved within the caller's bounded retry policy (visibly failed,
        never silently absent). Previously a chunk that came back short (one
        production incident parsed 1/10 submitted symbols) just had
        nothing in the dict for the other 9 — logged once at WARNING and
        otherwise indistinguishable from "never asked".
        """
        user_message = self.build_user_message(
            symbols_data=symbols_data,
            prior_ratings=prior_ratings or {},
            valuations=valuations or {},
            prior_macro_regime=prior_macro_regime,
            prior_macro_outlook=prior_macro_outlook,
            intraday_context=intraday_context or {},
            benchmark_pool=benchmark_pool or {},
        )
        result = self._execute(
            user_message,
            retry_kind="missing_symbol_recovery" if _is_logical_retry else None,
            optional_retry=_is_logical_retry,
            single_provider_attempt=_is_logical_retry,
        )
        parsed = result.parse_json()

        submitted = {s.get("symbol") for s in symbols_data if isinstance(s, dict)}
        # Index input by symbol so we can attach atr_14 back to each
        # TechAnalysisResult (the LLM doesn't echo ATR; we preserve it from
        # the indicators that fed the prompt so PortfolioConstructor's
        # fallback stop can be volatility-aware).
        input_indicators_by_sym: dict[str, float | None] = {}
        # The same levels the prompt was built from, kept so the constructor
        # can DERIVE the target rather than read the model's guess of it
        # (2026-09-01). Recomputed here rather than threaded out of
        # `build_user_message`: `find_structural_levels` is deterministic and
        # costs single-digit milliseconds, and that method is a public
        # prompt-rendering seam with snapshot tests over it — widening its
        # return type to smuggle data out would be the worse trade.
        computed_levels_by_sym: dict[str, list[float]] = {}
        for s in symbols_data:
            if not isinstance(s, dict):
                continue
            sym = s.get("symbol")
            indicators = s.get("indicators")
            if sym and indicators is not None:
                input_indicators_by_sym[sym] = getattr(indicators, "atr_14", None)
            bars = s.get("bars")
            if sym and bars:
                supports, resistances = find_structural_levels(bars)
                computed_levels_by_sym[sym] = sorted(
                    lv.price for lv in (*supports, *resistances)
                )

        analyses: dict[str, TechAnalysisResult] = {}
        failed_symbols: list[str] = []
        unsubmitted_symbols: list[str] = []

        if parsed is None:
            logger.error(
                "Tech analyst returned non-JSON for batch analysis (%d symbols "
                "submitted: %s)", len(submitted), sorted(submitted),
            )
        else:
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                try:
                    analysis = TechAnalysisResult(**item)
                    # audit round 2 #23: drop rows for symbols never submitted in
                    # this chunk. Production showed the LLM inventing phantom keys
                    # like "AAPL_CORRECTION" / "ZS_FINAL" — those rows leaked into
                    # PM/RM prompts and were persisted forever in the tech store,
                    # while the superseded original row survived as the real key.
                    if analysis.symbol not in submitted:
                        unsubmitted_symbols.append(analysis.symbol)
                        continue
                    # Carry ATR through from the input data (LLM doesn't emit it).
                    atr = input_indicators_by_sym.get(analysis.symbol)
                    if atr is not None:
                        analysis.atr_14 = atr
                    # Same treatment for the structural levels: computed in
                    # Python, carried on the result, never asked of the model.
                    analysis.computed_levels = computed_levels_by_sym.get(
                        analysis.symbol, [],
                    )
                    analyses[analysis.symbol] = analysis
                except Exception as e:
                    bad_symbol = str((item or {}).get("symbol", "?")) if isinstance(item, dict) else "?"
                    failed_symbols.append(bad_symbol)
                    # Counted, not just logged. A candidate the analysts
                    # researched and the Portfolio Manager never saw is
                    # under-deployment arriving by a side door, and until now
                    # the only trace was this log line — erased entirely from
                    # the operator's view whenever the retry below recovered
                    # the symbol and left data_status["tech"] reading "ok".
                    # Surfaced by RiskStage as the `analysis_parse_loss`
                    # advisory, the same non-blocking seam `data_degraded` and
                    # `pm_audit_step_missing` already use.
                    parse_telemetry.record_dropped_item("TechAnalysisResult", bad_symbol)
                    logger.error("Failed to parse tech analysis item for %s: %s", bad_symbol, e)
            if unsubmitted_symbols:
                logger.warning(
                    "Tech analyst emitted %d row(s) for symbols not in the submitted "
                    "chunk — dropped: %s", len(unsubmitted_symbols), unsubmitted_symbols,
                )

        missing = submitted - set(analyses.keys())
        retry_attempted = False
        if missing and _retries_left > 0:
            retry_attempted = True
            retry_data = [
                s for s in symbols_data
                if isinstance(s, dict) and s.get("symbol") in missing
            ]
            logger.warning(
                "Tech batch incomplete: submitted=%d, parsed=%d, validation-failed=%s, "
                "missing-from-response=%s — retrying the %d missing symbol(s) "
                "(%d retry attempt(s) left)",
                len(submitted), len(analyses), failed_symbols, sorted(missing),
                len(retry_data), _retries_left,
            )
            try:
                retry_analyses, retry_result = self._analyze_chunk(
                    retry_data, prior_ratings, valuations,
                    prior_macro_regime, prior_macro_outlook, intraday_context,
                    _retries_left=_retries_left - 1,
                    _is_logical_retry=True,
                )
            except OptionalPaidAnalysisRetrySkipped as exc:
                retry_analyses, retry_result = {}, None
                logger.warning(
                    "Tech recovery skipped without provider I/O; shared session "
                    "retry allowance is already spent: %s", exc.trigger,
                )
            except PaidAnalysisSuspended:
                raise
            except Exception as exc:
                retry_analyses, retry_result = {}, None
                logger.error(
                    "Tech optional recovery failed; retaining completed primary "
                    "analysis: %s", exc,
                )
            analyses.update({
                sym: a for sym, a in retry_analyses.items() if a is not None
            })
            if retry_result is not None:
                result = _merge_agent_results(result, retry_result)
            missing = submitted - set(analyses.keys())

        # Only the outermost call logs the final missing/resolved verdict —
        # a nested retry call already logged its own "retrying" warning
        # above, and would otherwise double-log the same symbols' final
        # outcome once per stack frame as the recursion unwinds.
        if _retries_left == _MAX_MISSING_RETRIES:
            if missing:
                logger.error(
                    "Tech batch: %d symbol(s) unresolved after%s — recording an "
                    "explicit failed outcome (never silently dropped): %s",
                    len(missing),
                    (
                        " retry" if retry_attempted else
                        " parsing (shared retry budget exhausted)"
                    ),
                    sorted(missing),
                )
            elif failed_symbols:
                logger.info(
                    "Tech batch: all %d initially-missing/invalid symbol(s) resolved "
                    "on retry: %s", len(failed_symbols), failed_symbols,
                )

        # Every submitted symbol is a key: TechAnalysisResult on success,
        # None for an explicit, visible, terminal failure — no key is ever
        # simply absent.
        out: dict[str, TechAnalysisResult | None] = {
            sym: analyses.get(sym) for sym in submitted
        }
        return out, result
