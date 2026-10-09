"""Provider prefixes, base URLs, provider resolution and fallback defaults.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""

from src.agents.llm_concurrency import _TOKEN_GOVERNORS


_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")

# DeepSeek is OpenAI-API-compatible: identical chat.completions wire format,
# reached through the openai SDK with a custom base_url + the DeepSeek key.
# Routed as a DISTINCT provider (not folded into _OPENAI_PREFIXES) because it
# needs (a) that base_url, (b) its own key, (c) the legacy `max_tokens` field —
# DeepSeek does NOT honor OpenAI's newer `max_completion_tokens`, so sending the
# latter is silently dropped and output falls back to a ~4096 default and
# truncates — and (d) a per-model output ceiling it REJECTS (does not clamp)
# values above. Verified against api-docs.deepseek.com 2026-06-05.
_DEEPSEEK_PREFIXES = ("deepseek-",)
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"  # no /v1, no trailing slash

# OpenRouter: also OpenAI-API-compatible (same chat.completions wire format),
# reached through the openai SDK with a custom base_url + the OpenRouter key —
# same shape as the DeepSeek branch above. Unlike OpenAI/DeepSeek/Anthropic,
# OpenRouter model ids are themselves "vendor/model" strings (e.g.
# "anthropic/claude-3.5-sonnet", "google/gemini-2.5-pro") that collide with
# native prefixes and can't be disambiguated by string inspection alone —
# routing to OpenRouter is therefore EXPLICIT-ONLY (see resolve_provider()
# below), never inferred from a prefix. Stage 1 (QAMC provider/model plumbing).
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Google AI Studio's OpenAI-compatible endpoint (2026-08-31 owner decision:
# gemini-3.5-flash-lite direct becomes the PRIMARY route for the eight
# specialist/review seats, free tier). Verified end-to-end from the box with
# streaming + usage — this is why `_call_openai`'s streamed OpenAI-wire path
# is reused (see _openai_wire_call) rather than a native Gemini client being
# written. The credential is injected as `Authorization: Bearer {value}`,
# which the OpenAI SDK sends natively via `api_key=` — no custom header work
# needed. NOTE: this is the COMPAT endpoint; the native Gemini REST endpoint
# uses `x-goog-api-key` instead and will NOT accept this same credential.
_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Bare Google-direct model ids (e.g. "gemini-3.5-flash-lite") are inferable
# from a prefix, unlike OpenRouter's "vendor/model" ids above — a config that
# names the model with no explicit `provider` must still route to Google
# rather than silently falling through to the Anthropic default.
_GOOGLE_PREFIXES = ("gemini-",)

# Per-model max-OUTPUT-token ceilings. We clamp client-side because DeepSeek
# rejects an over-ceiling max_tokens (HTTP 400/422 "Invalid max_tokens value")
# rather than silently clamping. The legacy deepseek-chat / deepseek-reasoner
# names now alias deepseek-v4-flash (1M ctx / 384K out) and are DEPRECATED
# 2026-07-24 — prefer configuring deepseek-v4-flash directly. An unknown
# deepseek-* id gets a conservative cap so a typo can't blow the ceiling.
# Source: official /pricing (v4 = 384K out) + create-chat-completion reference.
_DEEPSEEK_MAX_OUTPUT = {
    "deepseek-v4-flash": 384000,
    "deepseek-v4-pro": 384000,
    "deepseek-chat": 384000,  # legacy alias -> v4-flash (current routing)
    "deepseek-reasoner": 384000,  # legacy alias -> v4-flash (current routing)
}
_DEEPSEEK_DEFAULT_CEILING = 8192  # unknown deepseek-* id -> conservative cap

# One initial request plus one transient retry. The former seven-attempt loop
# amplified provider and validation failures into multi-dollar sessions. The
# persistent circuit below this layer also enforces a session-wide retry cap.


def _is_openai_model(model: str) -> bool:
    return any(model.startswith(p) for p in _OPENAI_PREFIXES)


def _governor_domain_for(model: str, agent, *, is_failover: bool = False, is_tertiary: bool = False) -> str:
    """Which provider's token budget this request will actually consume.

    Keyed on which PATH this attempt is taking, not on sniffing the model
    string: a cross-provider failover spends the FALLBACK provider's rate
    limit, not the primary's, and charging it to the wrong governor would let
    a failover storm slip past the ceiling it is supposed to be bounded by.

    ``is_failover`` is threaded explicitly from the call site (the dedicated
    failover authorize-closure in ``_execute()``) rather than inferred from
    the model text. Model-text sniffing was only ever safe because the old
    fallback model (``claude-opus-4-7``) had a distinctive "claude" prefix no
    primary model would ever share; now that the fallback (provider, model)
    pair is configurable, nothing guarantees the fallback model's spelling is
    distinctive — a Google-direct primary (bare id "gemini-3.5-flash-lite")
    and an OpenRouter-vendor-prefixed fallback of the "same" model
    ("google/gemini-3.5-flash-lite") happen to differ as strings, but that is
    incidental and nothing should depend on it.

    ``is_tertiary`` is the same argument one rung further down: route 3 is a
    different provider again, and charging its tokens to the SECONDARY's
    governor would both understate route 3's own rate usage and let a
    tertiary storm exhaust a ceiling that belongs to a route it is not using.
    Checked FIRST because a tertiary attempt is also flagged as a failover by
    the shared authorize closure.
    """
    if is_tertiary:
        tertiary_provider = getattr(agent, "_tertiary_provider", None)
        return tertiary_provider if tertiary_provider in _TOKEN_GOVERNORS else "openai"
    if is_failover:
        fallback_provider = getattr(agent, "_fallback_provider", None)
        return fallback_provider if fallback_provider in _TOKEN_GOVERNORS else "openrouter"
    provider = getattr(agent, "_provider", None)
    return provider if provider in _TOKEN_GOVERNORS else "openai"


def _is_deepseek_model(model: str) -> bool:
    return any(model.startswith(p) for p in _DEEPSEEK_PREFIXES)


def _is_google_model(model: str) -> bool:
    return any(model.startswith(p) for p in _GOOGLE_PREFIXES)


# Providers explicit config / _check_llm_provider_keys / pipeline.py are
# allowed to name. Anything else is treated as "no explicit override" so a
# typo can't silently misroute a live agent.
VALID_PROVIDERS = frozenset({"anthropic", "openai", "deepseek", "openrouter", "google"})


def _provider_for(model: str) -> str:
    """Provider implied by a model-id PREFIX alone (no explicit override).
    This is the pre-Stage-1 inference chain, extended (not otherwise changed)
    for Google-direct bare ids ("gemini-*") so every existing config keeps
    routing exactly as it did before Stage 1 / before Google was added."""
    if _is_openai_model(model):
        return "openai"
    if _is_deepseek_model(model):
        return "deepseek"
    if _is_google_model(model):
        return "google"
    return "anthropic"


def resolve_provider(model: str, explicit_provider: str | None = None) -> str:
    """Single source of truth for provider selection.

    An explicit provider always wins over prefix inference — required for
    OpenRouter, whose "vendor/model" ids (e.g. "anthropic/claude-3.5-sonnet")
    collide with native prefixes and cannot be told apart from the model
    string alone. When no (valid) explicit provider is given, falls back to
    the existing prefix chain unchanged, so every pre-Stage-1 config (no
    `provider` field set) routes identically to before.

    Reused by BaseAgent.__init__ (client construction), AppConfig's
    per-provider API-key validation, and pipeline.py's agent-key lookup, so
    those three call sites can never disagree about which provider a given
    (model, explicit_provider) pair means — a prior triplication risk this
    helper closes.
    """
    if explicit_provider:
        p = explicit_provider.strip().lower()
        if p in VALID_PROVIDERS:
            return p
    return _provider_for(model)


# Cross-provider failover target — CONFIGURABLE (2026-08-31 owner decision),
# not hardcoded. When a primary call ultimately fails — quota exhausted (the
# 2026-05-11 incident), DeepSeek 402 insufficient balance, a rate limit (the
# 2026-08-31 incident), dead key, sustained outage — the agent retries ONCE
# on the configured fallback (provider, model) so the trading session
# survives instead of dying (see BaseAgent._try_failover).
#
# These are process-wide DEFAULTS, threaded through pipeline.py from
# `config.llm.fallback_provider` / `fallback_model` (settings.yaml) into
# every BaseAgent.__init__ — not per-agent, so the fallback target can't
# silently drift seat-by-seat. The default pairs OpenRouter (paid, backup)
# with the SAME model Google AI Studio direct serves as the primary
# (gemini-3.5-flash-lite) for the eight specialist/review seats: a failover
# changes the ROAD, not the REASONING, which was the owner's explicit
# objection to the inherited claude-opus-4-7 Anthropic fallback (an
# upstream `yebof` remnant, commit d237f9b 2026-06-04, that nobody at QAMC
# chose) — a different model answering under stress is itself a source of
# surprise, on top of whatever the primary's outage already was.
_DEFAULT_FALLBACK_PROVIDER = "openrouter"
_DEFAULT_FALLBACK_MODEL = "google/gemini-3.5-flash-lite"
