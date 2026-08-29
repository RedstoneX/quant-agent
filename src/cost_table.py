"""Per-model LLM pricing for cost estimation.

Used by `src/agents/base.py` to compute per-call USD cost from the
input/output token counts returned by each provider, and by
`src/notifier.py` to surface session-level cost in Telegram pushes.

**Pricing source priority** (highest first):
  0. `_PRICING_PINNED` — verified-official rates for models where LiteLLM is
     KNOWN-STALE (currently DeepSeek). Structurally immune to cache refresh
     (`_apply_litellm_data` only iterates `_PRICING_FALLBACK` keys).
  0b. `_PRICING_OPENROUTER` — the models `config/settings.yaml` routes to
     OpenRouter, at OpenRouter's own published rates. Same structural
     immunity as `_PRICING_PINNED`, and for the same reason: LiteLLM prices
     the *vendor's direct* API, which is not what QAMC pays for
     OpenRouter-routed traffic, and it does not carry OpenRouter's
     `vendor/model` ids at all.
  1. `data/pricing_cache.json` — fetched from LiteLLM upstream JSON.
     Refreshed automatically every 24h via `refresh_pricing()` (also
     callable on-demand via `scripts/refresh_pricing.py`).
  2. `_PRICING_FALLBACK` — hand-curated baseline below. Used when
     network is unreachable AND no cache exists. **Verified against
     LiteLLM 2026-05-13** (gpt-5.5 / claude-opus-4-8 re-verified
     2026-06-05); numbers below are LiteLLM's snapshots at those dates.
  3. OpenRouter's live `/api/v1/models` catalog — on-demand, for a
     `vendor/model` id that is NOT in the pinned table (i.e. an operator
     experimenting with a model the policy has not adopted). Cached to
     `data/openrouter_pricing_cache.json` on the same 24h discipline.

On-demand resolution: `estimate_cost()` for a model in NEITHER the cache
nor `_PRICING_FALLBACK` triggers a one-time lookup against the LiteLLM
dataset (cache first, then a single live fetch only if the cache is stale/
missing). The resolved rate is memoised into `PRICING`; a model genuinely
absent from LiteLLM is memoised as unknown (so we never re-hit the network
for it) and renders as "$?.??" — never a fabricated price. This is what
lets a freshly-configured model (e.g. switching all agents to a new
`gpt-*`) report real cost on its first session without a code change.

The LiteLLM source (`model_prices_and_context_window.json`) is the
de-facto industry source for LLM pricing and is kept up to date by
the LiteLLM maintainers as providers publish changes. We pin to
their main branch raw URL and cache locally to avoid both startup
latency and silent breakage if they change paths.

Prices are USD per 1M tokens (the canonical unit on Anthropic and
OpenAI billing pages). LiteLLM's JSON stores per-TOKEN cost; we
multiply by 1,000,000 when ingesting.

Prompt-caching note: when Anthropic prompt caching is enabled, the
correct rates differ for cache-write (1.25×) and cache-read (0.1×).
Currently we don't use caching, so the simple input/output table
is sufficient.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# === LiteLLM remote pricing source ===
_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_CACHE_PATH = Path("data/pricing_cache.json")
_CACHE_MAX_AGE_SECONDS = 24 * 3600  # auto-refresh after 24h
_FETCH_TIMEOUT_S = 10.0

# === OpenRouter catalog — authoritative for what OpenRouter-routed calls cost ===
# Public endpoint, no key required (the commissioning preflight reads it the
# same way). Only consulted for `vendor/model` ids the pinned table lacks.
_OPENROUTER_PRICING_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_CACHE_PATH = Path("data/openrouter_pricing_cache.json")

# === Hardcoded baseline (last manual sync 2026-05-13 from LiteLLM) ===
# Used only when cache file is missing AND network fetch fails on
# first run. Verified-correct as of the sync date; later changes
# come in via cache refresh. Keys are exact model IDs as they
# appear in config/settings.yaml.
_PRICING_FALLBACK: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-8":     {"input":  5.00, "output": 25.00},  # verified LiteLLM 2026-06-05
    "claude-opus-4-7":     {"input":  5.00, "output": 25.00},  # current failover model
    "claude-sonnet-4-7":   {"input":  3.00, "output": 15.00},
    "claude-sonnet-4-6":   {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":    {"input":  1.00, "output":  5.00},

    # OpenAI
    "gpt-5.5":             {"input":  5.00, "output": 30.00},  # verified LiteLLM 2026-06-05 (current primary)
    "gpt-5.4":             {"input":  2.50, "output": 15.00},
    "gpt-5.2":             {"input":  1.75, "output": 14.00},
    "o4-mini":             {"input":  1.10, "output":  4.40},
}

# === Pinned overrides — verified-official rates that must BEAT LiteLLM ===
# Normally the LiteLLM cache (priority 1) wins over the hardcoded baseline. For
# these models LiteLLM is KNOWN-STALE and wrong, so we pin the official rate
# here instead. `_apply_litellm_data` only iterates `_PRICING_FALLBACK` keys, so
# anything in this dict is structurally immune to being overwritten by a cache
# load/refresh — and these are merged into PRICING at import below.
#
# DeepSeek (OpenAI-compatible): rates from the OFFICIAL /pricing page
# (api-docs.deepseek.com, 2026-06-05), cache-MISS input. LiteLLM's deepseek-chat/
# deepseek-reasoner rows ($0.28 in / $0.42 out) are a stale pre-V4 snapshot — if
# they won, output cost would be overstated ~50%. The names deepseek-chat /
# deepseek-reasoner are deprecated 2026-07-24 and now alias deepseek-v4-flash.
# NOTE: this flat table has no cache-tier column, so it can't represent
# DeepSeek's large context-cache discount (cache-hit input = $0.0028/M) → these
# rows OVER-estimate on cache-heavy runs. Conservative on purpose.
_PRICING_PINNED: dict[str, dict[str, float]] = {
    "deepseek-v4-flash":   {"input": 0.14,  "output": 0.28},
    "deepseek-v4-pro":     {"input": 0.435, "output": 0.87},
    "deepseek-chat":       {"input": 0.14,  "output": 0.28},   # legacy alias -> v4-flash
    "deepseek-reasoner":   {"input": 0.14,  "output": 0.28},   # legacy alias -> v4-flash
}

# === OpenRouter-routed models — OpenRouter's OWN published rates ===
# Every agent in config/settings.yaml runs `provider: openrouter`, so the
# rate that matters is OpenRouter's, not the vendor's direct API price. Two
# reasons this table has to exist rather than leaning on LiteLLM:
#
#   1. LiteLLM keys these as bare vendor ids ("gpt-5.5"), so an OpenRouter id
#      ("openai/gpt-5.5") missed every lookup and `estimate_cost` returned
#      None — the commissioned baseline was logging "$?.??" for EVERY agent
#      call, i.e. the deployment had no cost telemetry at all.
#   2. Where LiteLLM does carry a matching model, its rate is the vendor's
#      direct price. That is the wrong number for routed traffic.
#
# Rates are USD per 1M tokens, read from OpenRouter's /api/v1/models catalog
# on 2026-08-12 and reproducible with:
#     python ops/model_policy/verify_pricing.py
# which re-reads the catalog and fails if any row here has drifted.
#
# Note on caching: OpenRouter quotes a discounted `input_cache_read` rate for
# several of these. This flat table has no cache-tier column, so rows are the
# UNCACHED rate — cost is over-estimated on cache-heavy runs, never under.
# Only models the accepted policy actually routes to, plus the baseline the
# cost reduction is measured against. Rows for models nothing uses go stale
# unnoticed and make verify_pricing.py noisy; the on-demand catalog resolver
# below covers anything an operator wants to experiment with.
_PRICING_OPENROUTER: dict[str, dict[str, float]] = {
    # Seven specialist/review seats (docs/architecture/MODEL_ROUTING_POLICY.md).
    "google/gemini-2.5-flash-lite":    {"input": 0.100, "output":  0.400},
    # risk_manager only — held apart from PM's model for decision-chain
    # independence at measured-equal quality. Note the input rate is BELOW
    # gemini's: independence here costs nothing.
    "qwen/qwen3-235b-a22b-2507":       {"input": 0.090, "output":  0.550},
    # Commissioning baseline. Retained because it is what the cost reduction
    # is measured against, and pricing it is what makes that measurable.
    "openai/gpt-5.5":                  {"input": 5.000, "output": 30.000},
}

# Active PRICING — populated below from cache or fallback at module
# import time. Mutated in-place by refresh_pricing() so callers
# that imported the name `PRICING` see the latest values.
PRICING: dict[str, dict[str, float]] = {
    **_PRICING_FALLBACK, **_PRICING_PINNED, **_PRICING_OPENROUTER,
}


def _rates_from_entry(entry: object) -> dict[str, float] | None:
    """Validate a single LiteLLM model entry and convert its per-TOKEN rates
    to our per-MILLION-token units. Returns {'input':.., 'output':..} or None
    if the entry is missing / malformed / boolean / non-positive.

    Same validation rules as the inline checks in `_apply_litellm_data` (kept
    in sync deliberately): bool is a subclass of int so `True/False` rates are
    rejected, and a non-positive rate for a paid model is rejected so cost
    reporting can't be silently zeroed."""
    if not isinstance(entry, dict):
        return None
    in_rate = entry.get("input_cost_per_token")
    out_rate = entry.get("output_cost_per_token")
    if not (isinstance(in_rate, (int, float)) and isinstance(out_rate, (int, float))):
        return None
    if isinstance(in_rate, bool) or isinstance(out_rate, bool):
        return None
    if in_rate <= 0 or out_rate <= 0:
        return None
    return {"input": in_rate * 1_000_000, "output": out_rate * 1_000_000}


def _apply_litellm_data(data: dict) -> int:
    """Update PRICING in-place from a LiteLLM JSON payload. Returns
    number of models updated. Models that aren't in LiteLLM keep
    their fallback values — operator can extend _PRICING_FALLBACK
    for those, or LiteLLM may add them in a future commit."""
    # Defensive: cache file could have been hand-edited to `[]` for
    # debugging, or upstream LiteLLM schema could silently switch from
    # dict to list. Either crashes the caller chain (_load_cache or
    # refresh_pricing → main.py startup). Returning 0 lets the fallback
    # PRICING dict stay in effect.
    if not isinstance(data, dict):
        logger.warning(
            "LiteLLM payload not a dict (got %s) — skipping update",
            type(data).__name__,
        )
        return 0
    updated = 0
    # Iterate over our known keys plus any LiteLLM key that overlaps.
    # Use _PRICING_FALLBACK as the set of "models we care about" so
    # the in-memory PRICING dict doesn't get bloated with 2700 entries.
    for our_name in list(_PRICING_FALLBACK.keys()):
        entry = data.get(our_name)
        if not isinstance(entry, dict):
            continue
        in_rate = entry.get("input_cost_per_token")
        out_rate = entry.get("output_cost_per_token")
        if not (isinstance(in_rate, (int, float)) and isinstance(out_rate, (int, float))):
            continue
        # bool is a subclass of int — guard against `True/False` rates.
        if isinstance(in_rate, bool) or isinstance(out_rate, bool):
            continue
        # Reject non-positive rates. A paid LLM in our config can't be
        # legitimately $0 — if LiteLLM lists a "free" model with 0/0
        # rates, accepting it would silently zero out cost reporting
        # for any agent using that model. Operator should hand-curate
        # the rare free-tier case via _PRICING_FALLBACK instead.
        if in_rate <= 0 or out_rate <= 0:
            logger.warning(
                "LiteLLM rate for %s has non-positive value(s) "
                "(input=%s, output=%s) — skipping (would zero cost reporting)",
                our_name, in_rate, out_rate,
            )
            continue
        # LiteLLM stores per-TOKEN; convert to per-MILLION-tokens.
        PRICING[our_name] = {
            "input":  in_rate * 1_000_000,
            "output": out_rate * 1_000_000,
        }
        updated += 1
    return updated


def _load_cache() -> bool:
    """Apply cached pricing to PRICING. Returns True if any model
    was updated from cache."""
    if not _CACHE_PATH.exists():
        return False
    try:
        data = json.loads(_CACHE_PATH.read_text())
    except Exception as exc:
        logger.warning("pricing cache unreadable: %s", exc)
        return False
    n = _apply_litellm_data(data)
    if n:
        age_h = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600
        logger.info(
            "Loaded pricing from cache (%d models, cache age %.1fh)",
            n, age_h,
        )
    return n > 0


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age = time.time() - _CACHE_PATH.stat().st_mtime
    return age < _CACHE_MAX_AGE_SECONDS


def _fetch_litellm_dataset() -> dict | None:
    """Fetch the full LiteLLM pricing JSON and atomically cache it locally.

    Returns the parsed dict on success, or None on any network / HTTP /
    parse error (logged, never raised — the cost feature must never block
    trading if LiteLLM is unreachable). Shared by `refresh_pricing()`
    (bulk apply) and `_resolve_unknown_model()` (single-model lookup).
    """
    try:
        # Import requests lazily so that test environments without
        # network setup don't blow up at module import time.
        import requests
        resp = requests.get(_LITELLM_PRICING_URL, timeout=_FETCH_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("pricing fetch from LiteLLM failed: %s", exc)
        return None
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: dump to .tmp first, then rename. Prevents a
        # process-kill mid-write from leaving the cache file half-
        # serialised (next process would try to JSON-parse garbage,
        # log a warning, and fall through to network fetch — not
        # broken, just noisy). os.replace is atomic on POSIX +
        # within the same filesystem (which we always are here since
        # tmp + target are in the same data/ dir).
        tmp_path = _CACHE_PATH.with_suffix(_CACHE_PATH.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data))
        os.replace(str(tmp_path), str(_CACHE_PATH))
    except Exception as exc:
        logger.warning("pricing cache write failed: %s", exc)
    return data


def refresh_pricing(force: bool = False) -> bool:
    """Fetch latest LiteLLM pricing JSON and apply to PRICING.

    Skip the network call if a fresh cache (< 24h old) already exists,
    unless `force=True`. Caches the response to `data/pricing_cache.json`
    so subsequent process starts don't network. Returns True on success
    (PRICING was updated from network or fresh cache); False on
    network failure with no cache available (PRICING stays at
    last-known values, which is either previous cache or
    _PRICING_FALLBACK).

    Network errors are caught and logged — the cost feature must
    never block trading if LiteLLM is unreachable.
    """
    if not force and _cache_is_fresh():
        return _load_cache()
    data = _fetch_litellm_dataset()
    if data is None:
        # Network failed — any cache (even stale) beats nothing.
        if _CACHE_PATH.exists():
            return _load_cache()
        return False
    n = _apply_litellm_data(data)
    logger.info("Refreshed pricing from LiteLLM (%d/%d models matched)",
                n, len(_PRICING_FALLBACK))
    return n > 0


# Models confirmed ABSENT from the LiteLLM dataset after a real lookup —
# memoised so estimate_cost() doesn't re-hit cache/network for them on every
# call. Distinct from "lookup failed because the dataset was unreachable",
# which is NOT memoised (so it can succeed once connectivity returns).
_UNKNOWN_MODELS: set[str] = set()


def _read_cache_dataset() -> dict | None:
    """Return the full cached LiteLLM dataset (dict) or None if the cache is
    missing / unreadable / not a dict. No network, no freshness check."""
    if not _CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_CACHE_PATH.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _litellm_entry(data: dict, model: str) -> dict[str, float] | None:
    """Look up `model` in a LiteLLM dataset, trying the bare id first then the
    provider-prefixed forms LiteLLM occasionally uses as the canonical key
    (e.g. 'openai/<id>'). Returns validated per-million rates or None."""
    for key in (model, f"openai/{model}", f"anthropic/{model}"):
        rates = _rates_from_entry(data.get(key))
        if rates is not None:
            return rates
    return None


def _read_openrouter_cache() -> dict[str, dict[str, float]] | None:
    """Cached OpenRouter rate map, or None if missing/unreadable."""
    if not _OPENROUTER_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_OPENROUTER_CACHE_PATH.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _fetch_openrouter_pricing() -> dict[str, dict[str, float]] | None:
    """Fetch OpenRouter's catalog and cache a {model: rates} map.

    Only the id + the two rates are kept — the full catalog is ~2MB of
    descriptions and capability flags we have no use for, and a fat cache
    file is a fat thing to parse on every cold start.

    Never raises. General cost reporting may fall back when this returns
    ``None``; the mandatory paid-analysis preflight deliberately interprets
    ``None`` as a fail-closed prerequisite failure while non-LLM trading
    safety continues.
    """
    try:
        import requests
        resp = requests.get(_OPENROUTER_PRICING_URL, timeout=_FETCH_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("pricing fetch from OpenRouter failed: %s", exc)
        return None

    rates: dict[str, dict[str, float]] = {}
    for entry in (payload.get("data") or []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        try:
            # OpenRouter quotes per-TOKEN as strings; convert to per-MILLION.
            in_rate = float(pricing.get("prompt")) * 1_000_000
            out_rate = float(pricing.get("completion")) * 1_000_000
        except (TypeError, ValueError):
            continue
        # Same discipline as the LiteLLM path: a non-positive rate for a
        # model we'd actually run would silently zero out cost reporting.
        # OpenRouter's `:free` tiers legitimately quote 0 — excluding them
        # here means they render "$?.??" rather than a confident "$0.00"
        # that gets summed into daily totals.
        if in_rate <= 0 or out_rate <= 0:
            continue
        rates[model_id] = {"input": in_rate, "output": out_rate}

    if not rates:
        logger.warning("OpenRouter catalog parsed to zero priced models — ignoring")
        return None
    try:
        _OPENROUTER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _OPENROUTER_CACHE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(rates))
        os.replace(str(tmp_path), str(_OPENROUTER_CACHE_PATH))
    except Exception as exc:
        logger.warning("OpenRouter pricing cache write failed: %s", exc)
    return rates


def _openrouter_cache_is_fresh() -> bool:
    if not _OPENROUTER_CACHE_PATH.exists():
        return False
    age = time.time() - _OPENROUTER_CACHE_PATH.stat().st_mtime
    return age < _CACHE_MAX_AGE_SECONDS


def _openrouter_cache_age_hours() -> float | None:
    """Hours since the OpenRouter pricing cache file was last written, or
    ``None`` if it does not exist. Pure function of the file's current
    mtime -- no state is cached in this module, so a call made mid-session
    (e.g. from `openrouter_pricing_reservation_multiplier` below, once per
    LLM call) always reflects the file as it is right now, including a
    successful background refresh that landed since the session started."""
    if not _OPENROUTER_CACHE_PATH.exists():
        return None
    return (time.time() - _OPENROUTER_CACHE_PATH.stat().st_mtime) / 3600.0


# === Grace window for a stale-but-present OpenRouter cache (2026-08-28) ===
#
# The defect: `refresh_openrouter_pricing` accepted a cached rate ONLY under
# 24h old (`_openrouter_cache_is_fresh`). Past that boundary it had to reach
# openrouter.ai or return False, and both callers (`TradingPipeline.__init__`
# in src/pipeline.py and `activate_paid_call_session` below in
# src/cost_circuit.py) respond to False with `breaker.mark_unavailable(...)`
# -- the durable, cross-process emergency latch that only
# `LLMCostCircuitBreaker.reset()` (operator-only, reason mandatory) can
# clear. Because the cache is rewritten only when a fetch happens, and a
# fetch only happens once the cache is ALREADY stale, one openrouter.ai
# outage overlapping the first session past the 24h mark could latch every
# future session -- including the next day's -- until a human intervened.
# Reproduced 2026-08-28 via
# test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_is_down
# (cache 60s past the 24h boundary + `_fetch_openrouter_pricing` stubbed to
# fail => `refresh_openrouter_pricing()` returns False today).
#
# The fix distinguishes "just turned stale" from "genuinely unknown": model
# routing rates move on the order of once a quarter, and the figure only
# ever BOUNDS a reservation that already carries `reservation_multiplier` on
# top. So a cache within `grace_period_hours` of the 24h freshness boundary
# is used rather than latched -- with a widened multiplier (below) and a
# loud warning -- and only a cache older than that, or missing entirely, or
# lacking a rate for a model actually configured, still fails closed exactly
# as before 2026-08-28.
def refresh_openrouter_pricing(
    force: bool = False,
    *,
    grace_period_hours: float = 0.0,
    max_stale_multiplier: float = 1.0,
) -> bool:
    """Load current official rates for every accepted OpenRouter model.

    Unlike general cost telemetry, these rates are an input to the mandatory
    pre-call spending breaker.  A fresh (under 24h) official cache is valid
    outright.  Past that, the public catalog is tried live; if that fails,
    a cache still within `grace_period_hours` of the freshness boundary is
    accepted as a bounded (not unknown) rate -- see the module-level note
    above this function for why, and `openrouter_pricing_reservation_multiplier`
    for how the reservation is widened to compensate.  `grace_period_hours=0`
    (the default when a caller passes nothing, e.g. an old script or a test
    that predates 2026-08-28) reproduces the exact pre-fix behaviour: fail
    closed the instant the cache turns stale.  Production wiring
    (src/pipeline.py, src/cost_circuit.py's `activate_paid_call_session`)
    always passes the configured `llm_cost_circuit.openrouter_pricing_*`
    values explicitly.

    Beyond the grace window, with no cache at all, or with a cache that
    exists but lacks a valid rate for one of the accepted models, this
    returns False exactly as it always has -- that is genuinely unbounded
    cost, not merely stale, and the caller's fail-closed response
    (`mark_unavailable`) is correct for it.

    On success ``PRICING`` is updated in place so both reservations and
    post-call accounting use the same catalog snapshot.
    """

    rates = None
    stale_but_in_grace = False
    if not force and _openrouter_cache_is_fresh():
        rates = _read_openrouter_cache()
    if rates is None:
        rates = _fetch_openrouter_pricing()
    if rates is None and grace_period_hours > 0:
        # Live fetch failed (or was never attempted because a corrupt-but-
        # fresh cache read above also returned None -- either way we have no
        # confirmed-current rates). Fall back to whatever is on disk, but
        # ONLY if it is within the configured grace window; `age_hours` is
        # None when there is no cache file at all, which correctly skips
        # this branch and falls through to the fail-closed return below.
        age_hours = _openrouter_cache_age_hours()
        fresh_hours = _CACHE_MAX_AGE_SECONDS / 3600.0
        if age_hours is not None and age_hours <= fresh_hours + grace_period_hours:
            rates = _read_openrouter_cache()
            stale_but_in_grace = rates is not None
    if rates is None:
        logger.error(
            "OpenRouter pricing provenance unavailable; paid routed calls "
            "cannot be budgeted safely"
        )
        return False
    missing = [
        model for model in _PRICING_OPENROUTER
        if not _valid_rates(rates.get(model))
    ]
    if missing:
        logger.error(
            "OpenRouter catalog lacks valid rates for accepted models: %s",
            ", ".join(sorted(missing)),
        )
        return False
    for model in _PRICING_OPENROUTER:
        live = rates[model]
        PRICING[model] = {
            "input": float(live["input"]),
            "output": float(live["output"]),
        }
    if stale_but_in_grace:
        age_hours = _openrouter_cache_age_hours() or 0.0
        logger.warning(
            "OpenRouter catalog unreachable -- pricing %d accepted models from a "
            "STALE cache (%.1fh old, grace window %.1fh past the %.0fh freshness "
            "boundary). Reservations are widened toward %.2fx to compensate; "
            "trading continues. This is NOT the pre-2026-08-28 latch behaviour "
            "and is expected to self-clear on the next successful catalog fetch.",
            len(_PRICING_OPENROUTER), age_hours, grace_period_hours,
            _CACHE_MAX_AGE_SECONDS / 3600.0, max_stale_multiplier,
        )
    else:
        logger.info(
            "Verified current OpenRouter pricing for %d accepted models",
            len(_PRICING_OPENROUTER),
        )
    return True


def openrouter_pricing_reservation_multiplier(
    base_multiplier: float,
    *,
    grace_period_hours: float = 0.0,
    max_stale_multiplier: float = 1.0,
) -> float:
    """Widen `base_multiplier` in proportion to how stale the OpenRouter
    pricing cache currently is, within the configured grace window.

    Called per-call from `LLMCostCircuitBreaker._attempt_reserve`
    (src/cost_circuit.py) for any `vendor/model` id -- i.e. every
    OpenRouter-routed reservation, which per config/settings.yaml is every
    agent in production. Reads the cache file's mtime directly rather than
    remembering "was the last refresh stale": the cache does not change
    mid-session unless a later call's `refresh_openrouter_pricing` succeeds,
    so this stays correct across an entire session without extra state, and
    self-corrects the moment a fetch does succeed.

    Returns exactly `base_multiplier` (no widening) when: grace is disabled
    (`grace_period_hours <= 0`), there is no cache file, the cache is still
    within its 24h freshness window, or `max_stale_multiplier` is not
    actually above `base_multiplier` (config validation in src/config.py's
    `LLMCostCircuitConfig` already forbids that combination in production,
    but this function has no config object to trust and must not WIDEN
    downward on a malformed override).

    Otherwise scales LINEARLY from `base_multiplier` at the instant the
    cache turns stale (age == the 24h freshness boundary, fraction 0 -- no
    extra margin yet) up to `max_stale_multiplier` at the far edge of the
    grace window (fraction 1), continuous at both ends so there is no
    discontinuous jump the moment a cache crosses from fresh to stale. Any
    age beyond the grace window is clamped to fraction 1 -- this function
    only computes a multiplier; it does not decide whether stale-beyond-
    grace pricing may be used at all (`refresh_openrouter_pricing` already
    refused to load PRICING from it, so PRICING.get(model) is None by the
    time a reservation would reach this far in that case).
    """

    if grace_period_hours <= 0 or max_stale_multiplier <= base_multiplier:
        return base_multiplier
    age_hours = _openrouter_cache_age_hours()
    fresh_hours = _CACHE_MAX_AGE_SECONDS / 3600.0
    if age_hours is None or age_hours <= fresh_hours:
        return base_multiplier
    fraction = min(1.0, (age_hours - fresh_hours) / grace_period_hours)
    return base_multiplier + fraction * (max_stale_multiplier - base_multiplier)


def _memoise(model: str, rates: dict, source: str) -> dict[str, float]:
    PRICING[model] = {"input": float(rates["input"]), "output": float(rates["output"])}
    logger.info(
        "Resolved pricing for %s from %s: in=$%.3f/M out=$%.3f/M",
        model, source, PRICING[model]["input"], PRICING[model]["output"],
    )
    return PRICING[model]


def _valid_rates(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("input"), (int, float))
        and isinstance(value.get("output"), (int, float))
        and not isinstance(value.get("input"), bool)
        and not isinstance(value.get("output"), bool)
        and value["input"] > 0
        and value["output"] > 0
    )


def _resolve_openrouter_model_status(
    model: str,
) -> tuple[dict[str, float] | None, bool]:
    """`(rates_or_None, saw_catalog)` for a `vendor/model` id.

    The second element is what lets the caller tell "OpenRouter's catalog
    does not list this model" from "we could not reach OpenRouter's
    catalog". Both return no rates, but only the first is a permanent
    answer worth memoising — mirrors the `saw_dataset` discipline the
    LiteLLM path already uses.

    A FRESH cache is trusted outright. A stale one is not: it is kept only
    as the last resort if the refetch fails, because serving a rate that
    aged out of correctness is how a cost report becomes confidently wrong,
    and "wrong" is worse than the honest "$?.??" this returns instead.
    """
    cached = _read_openrouter_cache() or {}
    if _openrouter_cache_is_fresh():
        rates = cached.get(model)
        if _valid_rates(rates):
            return _memoise(model, rates, "cached OpenRouter catalog"), True
        return None, True

    fetched = _fetch_openrouter_pricing()
    if fetched is not None:
        rates = fetched.get(model)
        if _valid_rates(rates):
            return _memoise(model, rates, "live OpenRouter catalog"), True
        return None, True

    # Catalog unreachable. A stale entry beats no cost at all, and it is not
    # memoised as authoritative — the next process retries the fetch.
    rates = cached.get(model)
    if _valid_rates(rates):
        logger.warning(
            "OpenRouter catalog unreachable — pricing %s from a STALE cache "
            "(older than %dh); cost may be inaccurate",
            model, _CACHE_MAX_AGE_SECONDS // 3600,
        )
        return _memoise(model, rates, "stale OpenRouter cache"), False
    return None, False


def _resolve_openrouter_model(model: str) -> dict[str, float] | None:
    """Price a `vendor/model` id from OpenRouter's catalog.

    Reached only for ids NOT in `_PRICING_OPENROUTER` — i.e. a model an
    operator has configured but the accepted policy has not adopted.
    """
    return _resolve_openrouter_model_status(model)[0]


def _resolve_unknown_model(model: str) -> dict[str, float] | None:
    """First-use pricing lookup for a model not already in PRICING.

    Resolves from the LiteLLM dataset — the local cache first (no network),
    then exactly ONE live fetch, and only if the cache is stale or missing (a
    fresh cache that lacks the model means a re-fetch sees the same upstream
    snapshot, so we don't bother). On success the rate is memoised into
    PRICING so the lookup happens at most once. A model genuinely absent from
    a dataset we DID read is memoised into _UNKNOWN_MODELS. A failure caused
    purely by the dataset being unreachable is NOT memoised (retried later).
    Returns the rate dict, or None when the model can't be priced.

    A `vendor/model` id is resolved against OpenRouter's catalog and ONLY
    that catalog. It never falls through to LiteLLM, and that is a
    correctness rule rather than an optimisation:

    - LiteLLM keys models by bare vendor id, so the fall-through could only
      match by accident — and when it does match, `_litellm_entry` probes
      `<id>`, `openai/<id>` and `anthropic/<id>`, any of which can hit an
      unrelated row for a colliding id. `openai/gpt-5.5` is the live
      example: LiteLLM carries that exact key for OpenAI's DIRECT API,
      whose rate is not what routed OpenRouter traffic costs.
    - The failure mode is silent and confidently wrong. A cost report built
      on a direct-provider rate for routed traffic looks authoritative and
      is not, which is strictly worse than the "$?.??" an honest miss
      renders — the same argument this module already makes for refusing a
      stale OpenRouter rate.

    So an OpenRouter id that OpenRouter cannot price stays unpriced. When
    the catalog was actually read and did not list it, that is a permanent
    answer and is memoised; when the catalog was merely unreachable, it is
    not, so connectivity returning fixes it.
    """
    if not model or model in _UNKNOWN_MODELS:
        return None

    if "/" in model:
        rates, saw_catalog = _resolve_openrouter_model_status(model)
        if rates is not None:
            return rates
        if saw_catalog:
            _UNKNOWN_MODELS.add(model)
            logger.warning(
                "model %r is not priced in OpenRouter's catalog — cost will "
                "render as $?.?? rather than fall back to a direct-provider "
                "rate, which would not be what routed traffic costs. Add it "
                "to _PRICING_OPENROUTER if this is a real routed model.",
                model,
            )
        return None

    saw_dataset = False
    # 1. Local cache (full LiteLLM JSON written by refresh_pricing) — no network.
    cached = _read_cache_dataset()
    if cached is not None:
        saw_dataset = True
        rates = _litellm_entry(cached, model)
        if rates is not None:
            PRICING[model] = rates
            logger.info(
                "Resolved pricing for %s from cached LiteLLM data: "
                "in=$%.2f/M out=$%.2f/M", model, rates["input"], rates["output"],
            )
            return rates
    # 2. One live fetch — only if the cache is stale/missing (else re-fetching
    #    the same fresh snapshot can't surface a model the cache lacked).
    if not _cache_is_fresh():
        fresh = _fetch_litellm_dataset()
        if fresh is not None:
            saw_dataset = True
            rates = _litellm_entry(fresh, model)
            if rates is not None:
                PRICING[model] = rates
                logger.info(
                    "Resolved pricing for %s via live LiteLLM fetch: "
                    "in=$%.2f/M out=$%.2f/M", model, rates["input"], rates["output"],
                )
                return rates
    if saw_dataset:
        # We read a dataset and the model genuinely isn't in it — stop checking.
        _UNKNOWN_MODELS.add(model)
        logger.warning(
            "model %r not found in LiteLLM pricing dataset — cost will render "
            "as $?.??; add it to _PRICING_FALLBACK if this is a real model",
            model,
        )
    return None


# Auto-load at import. Reads existing cache if any; does NOT auto-fetch
# (network at import time is a recipe for slow tests and surprise
# failures). Explicit refresh_pricing() call is the entry point —
# main.py wires it on startup.
_load_cache()


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Return USD cost for one LLM call. None if the model is unknown.

    Caller treats None as "couldn't compute" and should fall back to
    logging just the token counts (don't fabricate a $0.00 — that
    would misrepresent in aggregations).

    Cost = (input_tokens * input_rate + output_tokens * output_rate)
    rates in USD-per-million tokens; result in USD.

    A model not in PRICING triggers a one-time on-demand lookup against the
    LiteLLM dataset (see `_resolve_unknown_model`) so a newly-configured model
    reports real cost without a code change. A model that lookup can't price
    still returns None (we never fabricate a rate).
    """
    if input_tokens < 0 or output_tokens < 0:
        return None
    rates = PRICING.get(model)
    if rates is None:
        rates = _resolve_unknown_model(model)
    if rates is None:
        return None
    return (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
    ) / 1_000_000.0


def fmt_cost(cost_usd: float | None) -> str:
    """Render a cost value for human-readable logs / messages.

    None → '$?.??' (unknown model — flag for operator review).
    Sub-cent values → 4-decimal precision (e.g. $0.0042) since per-call
    costs for cheap agents (macro / news / position_reviewer) are
    in the millicent range.
    Cent+ values → 2-decimal (e.g. $0.85, $14.32).
    """
    if cost_usd is None:
        return "$?.??"
    # Render exact-zero as "$0.00" — same shape as everything ≥$0.01.
    # The 4-decimal sub-cent branch below would yield "$0.0000" which
    # looks inconsistent next to "$0.30 (3 calls)" in a Telegram line.
    if cost_usd == 0.0:
        return "$0.00"
    if cost_usd < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:,.2f}"
