"""src/cost_table.py: per-model pricing + estimate_cost + fmt_cost."""
from src.cost_table import PRICING, estimate_cost, fmt_cost


def test_estimate_cost_claude_opus_47_round_trip():
    """Sanity: 1M input + 1M output on the default model = input_rate + output_rate.
    Catches off-by-1000x errors that have bitten cost calcs in other codebases."""
    cost = estimate_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    expected = PRICING["claude-opus-4-7"]["input"] + PRICING["claude-opus-4-7"]["output"]
    assert cost == expected


def test_estimate_cost_realistic_pm_call():
    """Typical PM call: ~50k input, ~2k output.
    Math uses whatever PRICING currently has — values verified
    against LiteLLM 2026-05-13 for claude-opus-4-7 ($5/$25 per M)."""
    cost = estimate_cost("claude-opus-4-7", 50_000, 2_000)
    rates = PRICING["claude-opus-4-7"]
    expected = (50_000 * rates["input"] + 2_000 * rates["output"]) / 1_000_000
    assert abs(cost - expected) < 1e-9
    # Sanity-bound: PM call should land somewhere in $0.10-$1.50 range
    # whichever pricing tier the user is on. If it's outside, the
    # PRICING table has wildly wrong rates (or the test is stale).
    assert 0.10 < cost < 1.50


def test_estimate_cost_realistic_tech_call():
    """Tech analyst chunked: ~80k input, ~30k output per chunk."""
    cost = estimate_cost("claude-opus-4-7", 80_000, 30_000)
    rates = PRICING["claude-opus-4-7"]
    expected = (80_000 * rates["input"] + 30_000 * rates["output"]) / 1_000_000
    assert abs(cost - expected) < 1e-9
    # Sanity-bound: tech chunk should land somewhere in $0.50-$5 range.
    assert 0.50 < cost < 5.00


def test_estimate_cost_unknown_model_returns_none():
    """An agent on a non-listed model must not silently produce $0 —
    return None so the caller surfaces '$?.??' and the operator
    knows to update cost_table.PRICING."""
    assert estimate_cost("not-a-real-model", 1000, 100) is None
    assert estimate_cost("", 1000, 100) is None


def test_estimate_cost_negative_tokens_returns_none():
    """Defensive: negative token counts (corrupt SDK response) shouldn't
    produce a negative 'rebate'. Return None to flag the bug instead."""
    assert estimate_cost("claude-opus-4-7", -10, 100) is None
    assert estimate_cost("claude-opus-4-7", 10, -100) is None


def test_estimate_cost_zero_tokens_is_zero():
    """A model call that returned 0 tokens (very rare, broker hiccup
    or empty content path) costs $0 — not None.

    NOTE: at the calling layer (base.py:run), 0+0 tokens is treated
    as "missing usage data" and cost is forced to None at THAT
    layer — see the WARN log. This test pins estimate_cost itself
    to return 0.0 for 0/0 input (mathematically correct);
    the surface-API semantics are tested elsewhere."""
    assert estimate_cost("claude-opus-4-7", 0, 0) == 0.0


def test_estimate_cost_haiku_significantly_cheaper():
    """Sanity: Haiku 4.5 should be ~10-20x cheaper per token than Opus
    on both input and output. Catches accidental row swap in PRICING."""
    cost_opus = estimate_cost("claude-opus-4-7", 100_000, 10_000)
    cost_haiku = estimate_cost("claude-haiku-4-5", 100_000, 10_000)
    assert cost_haiku < cost_opus * 0.3, (
        f"Haiku should be much cheaper than Opus; "
        f"got opus=${cost_opus}, haiku=${cost_haiku}"
    )


def test_fmt_cost_renders_none_as_unknown():
    assert fmt_cost(None) == "$?.??"


def test_fmt_cost_sub_cent_keeps_four_decimals():
    """Cheap agent calls (macro / news on Haiku) can be ~$0.0005.
    Two-decimal format would round to $0.00 and look like a bug."""
    assert fmt_cost(0.0042) == "$0.0042"
    assert fmt_cost(0.0001) == "$0.0001"


def test_fmt_cost_dollar_plus_uses_two_decimals_with_separator():
    assert fmt_cost(1.234) == "$1.23"
    assert fmt_cost(14.789) == "$14.79"
    assert fmt_cost(1234.5) == "$1,234.50"


# === Defensive token extraction (R7 audit follow-up) ===

def test_extract_anthropic_usage_handles_missing_usage_object():
    """Some Anthropic SDK error paths return a response with no .usage
    attribute. Old code crashed AttributeError on response.usage.input_tokens.
    Pin: helper returns (0, 0) and the run() layer then flags cost=None."""
    from src.agents.base import _extract_anthropic_usage
    from unittest.mock import MagicMock

    response_no_usage = MagicMock(spec=["content", "stop_reason"])
    # MagicMock(spec=[...]) raises AttributeError for any other attr,
    # mimicking a usage-less response.
    assert _extract_anthropic_usage(response_no_usage, "test_agent") == (0, 0)


def test_extract_anthropic_usage_sums_cache_tokens():
    """When prompt caching is enabled in a future change, Anthropic
    splits input tokens into input_tokens (uncached) +
    cache_creation_input_tokens + cache_read_input_tokens. The token
    COUNT we record needs all three for correct total accounting,
    even though cost-rate math would later need separate handling."""
    from src.agents.base import _extract_anthropic_usage
    from types import SimpleNamespace

    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=5000,
            cache_creation_input_tokens=2000,
            cache_read_input_tokens=8000,
            output_tokens=1000,
        ),
    )
    in_tok, out_tok = _extract_anthropic_usage(response, "test_agent")
    assert in_tok == 5000 + 2000 + 8000  # all three input fields summed
    assert out_tok == 1000


def test_extract_openai_usage_handles_missing_usage_object():
    """OpenAI: response.usage can be None on some error paths. Pre-fix
    code silently returned (0, 0) and then cost=$0 landed in DB →
    silently understated daily totals. Now we still return (0, 0)
    but emit a WARN and the run() layer flags cost=None."""
    from src.agents.base import _extract_openai_usage
    from types import SimpleNamespace

    response = SimpleNamespace(usage=None)
    assert _extract_openai_usage(response, "test_agent") == (0, 0)


def test_extract_openai_usage_normal_path():
    from src.agents.base import _extract_openai_usage
    from types import SimpleNamespace

    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=5000, completion_tokens=1000),
    )
    assert _extract_openai_usage(response, "test_agent") == (5000, 1000)


# === LiteLLM pricing refresh (R7 follow-up: prices must come from upstream) ===

def test_apply_litellm_data_converts_per_token_to_per_million(monkeypatch):
    """LiteLLM stores cost per single token; our PRICING uses per-million-token
    units so the math in estimate_cost is readable. Pin the conversion."""
    from src.cost_table import _apply_litellm_data, PRICING

    # Snapshot original so we can restore after.
    original = {k: dict(v) for k, v in PRICING.items()}
    try:
        # Realistic LiteLLM shape — they store cost per token (not per million).
        fake_data = {
            "claude-opus-4-7": {
                "input_cost_per_token": 5e-6,    # $5 / M
                "output_cost_per_token": 25e-6,  # $25 / M
                "max_input_tokens": 200000,      # ignored by us
            },
        }
        n = _apply_litellm_data(fake_data)
        assert n == 1
        assert PRICING["claude-opus-4-7"]["input"] == 5.0
        assert PRICING["claude-opus-4-7"]["output"] == 25.0
    finally:
        # Restore so other tests don't see leaked mutations.
        PRICING.clear()
        PRICING.update(original)


def test_apply_litellm_data_skips_negative_or_non_numeric_rates(monkeypatch):
    """Defensive: a corrupt LiteLLM entry (negative price, string, missing
    field) must be skipped — keep the prior PRICING entry intact."""
    from src.cost_table import _apply_litellm_data, PRICING

    original = {k: dict(v) for k, v in PRICING.items()}
    try:
        bad_data = {
            "claude-opus-4-7": {"input_cost_per_token": -1, "output_cost_per_token": 25e-6},
            "claude-sonnet-4-6": {"input_cost_per_token": "oops"},
            "claude-haiku-4-5": {},  # missing both fields
        }
        n = _apply_litellm_data(bad_data)
        assert n == 0
        # Original values must remain.
        assert PRICING["claude-opus-4-7"] == original["claude-opus-4-7"]
        assert PRICING["claude-sonnet-4-6"] == original["claude-sonnet-4-6"]
        assert PRICING["claude-haiku-4-5"] == original["claude-haiku-4-5"]
    finally:
        PRICING.clear()
        PRICING.update(original)


def test_refresh_pricing_falls_back_to_cache_when_network_fails(tmp_path, monkeypatch):
    """If the LiteLLM fetch raises (DNS / 5xx / firewall), refresh
    must NOT crash and must fall back to the existing cache file.
    Trading is observability-only here — pricing fetch failure is
    non-fatal."""
    import json as _json
    from src import cost_table

    # Redirect cache to a temp path with a known-good snapshot.
    cache = tmp_path / "pricing_cache.json"
    cache.write_text(_json.dumps({
        "claude-opus-4-7": {
            "input_cost_per_token": 7e-6,
            "output_cost_per_token": 33e-6,
        },
    }))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_CACHE_MAX_AGE_SECONDS", 0)  # always stale

    # Make `requests.get` raise.
    import requests
    def _explode(*a, **kw):
        raise requests.ConnectionError("simulated DNS failure")
    monkeypatch.setattr("src.cost_table.requests.get" if False else "requests.get", _explode)

    original = {k: dict(v) for k, v in cost_table.PRICING.items()}
    try:
        ok = cost_table.refresh_pricing(force=True)
        # Returns True because cache was successfully loaded as fallback.
        assert ok is True
        # PRICING was updated from cache (the unusual 7/33 numbers).
        assert cost_table.PRICING["claude-opus-4-7"]["input"] == 7.0
        assert cost_table.PRICING["claude-opus-4-7"]["output"] == 33.0
    finally:
        cost_table.PRICING.clear()
        cost_table.PRICING.update(original)


def test_apply_litellm_data_rejects_zero_rates(monkeypatch):
    """A LiteLLM entry with input or output rate = 0 must be rejected.
    Free-tier models in our config would otherwise silently report
    $0 cost, hiding real usage from cost monitoring (and from the
    operator's Telegram push). If LiteLLM ever flags a model as
    free during a paid-tier transition, the fallback rate wins
    until operator updates _PRICING_FALLBACK explicitly."""
    from src.cost_table import _apply_litellm_data, PRICING

    original = {k: dict(v) for k, v in PRICING.items()}
    try:
        zero_rates = {
            "claude-opus-4-7": {
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
            },
            "claude-haiku-4-5": {
                "input_cost_per_token": 1e-6,
                "output_cost_per_token": 0.0,  # only output is 0
            },
        }
        n = _apply_litellm_data(zero_rates)
        assert n == 0
        # Both fallbacks intact.
        assert PRICING["claude-opus-4-7"] == original["claude-opus-4-7"]
        assert PRICING["claude-haiku-4-5"] == original["claude-haiku-4-5"]
    finally:
        PRICING.clear()
        PRICING.update(original)


def test_apply_litellm_data_rejects_bool_rates(monkeypatch):
    """`True == 1` and `isinstance(True, int) == True` in Python. If a
    LiteLLM corruption produced True/False in a rate field, our code
    must reject it instead of coercing to a 1.0/0.0 cost rate."""
    from src.cost_table import _apply_litellm_data, PRICING

    original = {k: dict(v) for k, v in PRICING.items()}
    try:
        bool_rates = {
            "claude-opus-4-7": {
                "input_cost_per_token": True,
                "output_cost_per_token": 5e-6,
            },
        }
        n = _apply_litellm_data(bool_rates)
        assert n == 0
        assert PRICING["claude-opus-4-7"] == original["claude-opus-4-7"]
    finally:
        PRICING.clear()
        PRICING.update(original)


def test_refresh_pricing_atomic_write(tmp_path, monkeypatch):
    """Cache write must be atomic — a process-kill mid-write must not
    leave the cache file half-serialised. Pin: write goes via .tmp +
    rename so either the new content is fully visible or the old
    content is still in place."""
    import json as _json
    from src import cost_table

    cache = tmp_path / "pricing_cache.json"
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)

    fake_payload = {
        "claude-opus-4-7": {
            "input_cost_per_token": 5e-6,
            "output_cost_per_token": 25e-6,
        },
    }

    # Mock requests.get to return our fake payload.
    import requests
    class _R:
        def raise_for_status(self): pass
        def json(self): return fake_payload
    monkeypatch.setattr("requests.get", lambda *a, **k: _R())

    original = {k: dict(v) for k, v in cost_table.PRICING.items()}
    try:
        ok = cost_table.refresh_pricing(force=True)
        assert ok is True
        # Cache file exists and is valid JSON.
        assert cache.exists()
        loaded = _json.loads(cache.read_text())
        assert loaded == fake_payload
        # .tmp file should NOT linger (os.replace moved it).
        assert not cache.with_suffix(cache.suffix + ".tmp").exists()
    finally:
        cost_table.PRICING.clear()
        cost_table.PRICING.update(original)


def test_refresh_pricing_returns_false_when_no_cache_and_network_fails(tmp_path, monkeypatch):
    """No cache + no network = honest False return so the operator
    knows the fetch didn't update anything. PRICING stays at
    whatever was loaded at module import (the fallback)."""
    from src import cost_table

    cache = tmp_path / "pricing_cache.json"  # does NOT exist
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)

    import requests
    def _explode(*a, **kw):
        raise requests.ConnectionError("no network")
    monkeypatch.setattr("requests.get", _explode)

    assert cost_table.refresh_pricing(force=True) is False


def test_apply_litellm_data_rejects_non_dict_payload(monkeypatch):
    """LiteLLM's main-branch JSON IS a dict at the top level. If a future
    refactor on their end (or a corrupted local cache like
    `echo "[]" > data/pricing_cache.json`) yields a list/null/string
    instead, the iterator `data.get(name)` raises AttributeError and
    crashes the caller chain — and `_load_cache()` runs at module
    import, so this would brick main.py startup. Pin: non-dict is a
    silent skip with a warning, PRICING stays at fallback."""
    from src.cost_table import _apply_litellm_data, PRICING

    original = {k: dict(v) for k, v in PRICING.items()}
    try:
        # All four realistic non-dict shapes JSON can decode to.
        assert _apply_litellm_data([]) == 0
        assert _apply_litellm_data([{"x": 1}]) == 0
        assert _apply_litellm_data(None) == 0
        assert _apply_litellm_data("garbage") == 0
        assert _apply_litellm_data(42) == 0
        # PRICING untouched by any of the failed calls.
        assert PRICING == original
    finally:
        PRICING.clear()
        PRICING.update(original)


def test_fmt_cost_zero_uses_two_decimal_consistent_with_cents():
    """fmt_cost(0.0) must render as "$0.00" — same shape as everything
    ≥$0.01. Pre-fix it returned "$0.0000" because 0.0 < 0.01 fell into
    the sub-cent branch, which looked inconsistent next to "$0.30 (3 calls)"
    in Telegram lines. Sub-cent POSITIVE values keep 4-decimal precision."""
    from src.cost_table import fmt_cost
    assert fmt_cost(0.0) == "$0.00"
    # Sub-cent positives keep precision so $0.0001 doesn't round to $0.00.
    assert fmt_cost(0.0001) == "$0.0001"
    assert fmt_cost(0.005) == "$0.0050"
    # Boundary: exactly $0.01 uses 2-decimal.
    assert fmt_cost(0.01) == "$0.01"


# === On-demand resolution of unfamiliar models (the gpt-5.5 "$?.??" bug) ===
import json as _json_mod
import pytest


@pytest.fixture
def _restore_pricing():
    """Snapshot + restore the mutable module globals (PRICING and the
    _UNKNOWN_MODELS memo) so on-demand-resolution tests don't leak resolved
    rates / negative memos into the rest of the session."""
    from src import cost_table
    pricing_snap = {k: dict(v) for k, v in cost_table.PRICING.items()}
    unknown_snap = set(cost_table._UNKNOWN_MODELS)
    yield
    cost_table.PRICING.clear()
    cost_table.PRICING.update(pricing_snap)
    cost_table._UNKNOWN_MODELS.clear()
    cost_table._UNKNOWN_MODELS.update(unknown_snap)


def test_gpt_5_5_prices_from_baseline():
    """THE reported bug: every agent is on gpt-5.5 and the push showed
    '$?.??'. gpt-5.5 must now produce a real cost (it's in _PRICING_FALLBACK
    and in LiteLLM at $5/$30 per M, verified 2026-06-05)."""
    cost = estimate_cost("gpt-5.5", 1_000_000, 1_000_000)
    assert cost is not None
    assert cost == PRICING["gpt-5.5"]["input"] + PRICING["gpt-5.5"]["output"]
    assert 20.0 < cost < 60.0  # sanity: not a 1000x unit error


def test_claude_opus_4_8_prices():
    """The current/newest Claude (and config.py default) must be priced too."""
    cost = estimate_cost("claude-opus-4-8", 1_000_000, 0)
    assert cost is not None
    assert cost == PRICING["claude-opus-4-8"]["input"]


def test_resolves_unknown_model_from_cache_without_network(tmp_path, monkeypatch, _restore_pricing):
    """A model in NEITHER fallback NOR PRICING but present in the local
    LiteLLM cache resolves from cache — and must NOT touch the network."""
    from src import cost_table
    cache = tmp_path / "pricing_cache.json"
    cache.write_text(_json_mod.dumps(
        {"nova-test-1": {"input_cost_per_token": 5e-6, "output_cost_per_token": 30e-6}}
    ))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)

    def _boom():
        raise AssertionError("cache hit must not trigger a network fetch")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", _boom)

    cost = cost_table.estimate_cost("nova-test-1", 1_000_000, 1_000_000)
    assert cost == 35.0  # 5 + 30 per M
    # memoised into PRICING so the next call is a plain dict hit
    assert cost_table.PRICING["nova-test-1"] == {"input": 5.0, "output": 30.0}


def test_resolves_unknown_model_via_live_fetch_when_cache_missing(tmp_path, monkeypatch, _restore_pricing):
    """No cache (e.g. fresh CI checkout / a brand-new model) → exactly one
    live fetch resolves it. This is 'look it up on first run'."""
    from src import cost_table
    monkeypatch.setattr(cost_table, "_CACHE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(
        cost_table, "_fetch_litellm_dataset",
        lambda: {"nova-test-2": {"input_cost_per_token": 5e-6, "output_cost_per_token": 30e-6}},
    )
    cost = cost_table.estimate_cost("nova-test-2", 1_000_000, 1_000_000)
    assert cost == 35.0
    assert "nova-test-2" in cost_table.PRICING


def test_unknown_model_absent_from_dataset_is_memoised(tmp_path, monkeypatch, _restore_pricing):
    """A model genuinely not in LiteLLM → None (honest '$?.??', never a
    fabricated price) and is memoised so we never re-read for it."""
    from src import cost_table
    cache = tmp_path / "pricing_cache.json"
    cache.write_text(_json_mod.dumps(
        {"some-other": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}
    ))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)  # fresh (just written)

    assert cost_table.estimate_cost("ghost-model", 100, 50) is None
    assert "ghost-model" in cost_table._UNKNOWN_MODELS

    # Second call must short-circuit on the memo — prove no cache re-read.
    def _boom():
        raise AssertionError("memoised miss must not re-read the dataset")
    monkeypatch.setattr(cost_table, "_read_cache_dataset", _boom)
    assert cost_table.estimate_cost("ghost-model", 100, 50) is None


def test_unknown_model_not_memoised_when_dataset_unreachable(tmp_path, monkeypatch, _restore_pricing):
    """If the lookup fails only because the dataset is unreachable (no cache +
    network down), do NOT memoise — so it can resolve once connectivity
    returns instead of being stuck at '$?.??' forever."""
    from src import cost_table
    monkeypatch.setattr(cost_table, "_CACHE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", lambda: None)  # network down
    assert cost_table.estimate_cost("temp-outage-model", 100, 50) is None
    assert "temp-outage-model" not in cost_table._UNKNOWN_MODELS


def test_fresh_cache_lacking_model_skips_redundant_fetch(tmp_path, monkeypatch, _restore_pricing):
    """A FRESH cache that lacks the model must NOT trigger a fetch (re-fetching
    the same upstream snapshot can't surface it) — it's memoised as unknown."""
    from src import cost_table
    cache = tmp_path / "pricing_cache.json"
    cache.write_text(_json_mod.dumps({"x": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)

    def _boom():
        raise AssertionError("fresh cache lacking the model must not fetch")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", _boom)
    assert cost_table.estimate_cost("not-in-fresh-cache", 100, 50) is None


def test_litellm_entry_resolves_provider_prefixed_key():
    """LiteLLM sometimes keys a model as 'openai/<id>' rather than the bare id.
    The resolver tries provider-prefixed variants."""
    from src.cost_table import _litellm_entry
    data = {"openai/exotic-model": {"input_cost_per_token": 2e-6, "output_cost_per_token": 4e-6}}
    assert _litellm_entry(data, "exotic-model") == {"input": 2.0, "output": 4.0}


def test_rates_from_entry_validation():
    """The shared validator rejects malformed / bool / non-positive entries
    and converts per-token → per-million on the happy path."""
    from src.cost_table import _rates_from_entry
    assert _rates_from_entry({"input_cost_per_token": 5e-6, "output_cost_per_token": 30e-6}) == {"input": 5.0, "output": 30.0}
    assert _rates_from_entry({"input_cost_per_token": 0, "output_cost_per_token": 30e-6}) is None  # non-positive
    assert _rates_from_entry({"input_cost_per_token": True, "output_cost_per_token": 30e-6}) is None  # bool
    assert _rates_from_entry({"input_cost_per_token": "x", "output_cost_per_token": 30e-6}) is None  # non-numeric
    assert _rates_from_entry({"input_cost_per_token": 5e-6}) is None  # missing output
    assert _rates_from_entry(None) is None
    assert _rates_from_entry("garbage") is None


def test_deepseek_models_priced_from_official_rates():
    """DeepSeek rows use the OFFICIAL /pricing rates ($0.14/$0.28), pinned so the
    STALE LiteLLM snapshot ($0.28/$0.42, which IS in the cache for deepseek-chat/
    -reasoner) can't clobber them on a refresh."""
    assert abs(estimate_cost("deepseek-v4-flash", 1_000_000, 1_000_000) - (0.14 + 0.28)) < 1e-9
    # legacy aliases priced same as v4-flash (they route to it) — pinned, NOT the
    # LiteLLM $0.28 input that would win without the pin.
    assert abs(estimate_cost("deepseek-chat", 1_000_000, 0) - 0.14) < 1e-9
    assert abs(estimate_cost("deepseek-reasoner", 1_000_000, 0) - 0.14) < 1e-9
    # pro tier is more expensive
    assert abs(estimate_cost("deepseek-v4-pro", 0, 1_000_000) - 0.87) < 1e-9


def test_deepseek_pinned_survives_cache_refresh(tmp_path, monkeypatch, _restore_pricing):
    """A cache refresh carrying LiteLLM's stale deepseek-chat ($0.28/$0.42) must
    NOT overwrite the pinned official rate."""
    from src import cost_table
    cache = tmp_path / "pricing_cache.json"
    monkeypatch.setattr(cost_table, "_CACHE_PATH", cache)
    monkeypatch.setattr(
        cost_table, "_fetch_litellm_dataset",
        lambda: {"deepseek-chat": {"input_cost_per_token": 0.28e-6, "output_cost_per_token": 0.42e-6}},
    )
    cost_table.refresh_pricing(force=True)
    assert cost_table.PRICING["deepseek-chat"] == {"input": 0.14, "output": 0.28}  # pin held


# --------------------------------------------------------------------------
# OpenRouter-routed pricing (the commissioned posture: every agent is on
# `provider: openrouter`, so every model id is a "vendor/model" string)
# --------------------------------------------------------------------------


def test_openrouter_ids_are_priced_not_unknown():
    """THE commissioning gap: `estimate_cost("openai/gpt-5.5", ...)` returned
    None, so a deployment where every agent runs on OpenRouter logged
    "$?.??" for every single call — i.e. no cost telemetry at all."""
    cost = estimate_cost("openai/gpt-5.5", 1_000_000, 1_000_000)
    assert cost is not None
    assert abs(cost - (5.0 + 30.0)) < 1e-9


def test_openrouter_policy_models_are_all_priced():
    """Every model the accepted policy can route to must price offline —
    cost reporting cannot depend on reaching a catalog mid-session."""
    from src.cost_table import _PRICING_OPENROUTER
    for model in _PRICING_OPENROUTER:
        assert estimate_cost(model, 1000, 1000) is not None, model


def test_openrouter_rates_beat_litellm_for_routed_traffic(monkeypatch, _restore_pricing):
    """LiteLLM prices the vendor's DIRECT API. For OpenRouter-routed traffic
    that is the wrong number, so a cache refresh must not be able to move a
    pinned OpenRouter row."""
    from src import cost_table
    monkeypatch.setattr(
        cost_table, "_fetch_litellm_dataset",
        lambda: {"openai/gpt-5.5": {"input_cost_per_token": 99e-6,
                                    "output_cost_per_token": 99e-6}},
    )
    cost_table.refresh_pricing(force=True)
    assert cost_table.PRICING["openai/gpt-5.5"] == {"input": 5.0, "output": 30.0}


def test_unpinned_openrouter_id_resolves_from_catalog_cache(tmp_path, monkeypatch, _restore_pricing):
    """An operator experimenting with a model the policy hasn't adopted still
    gets a real cost — from OpenRouter's catalog, without a network call."""
    from src import cost_table
    cache = tmp_path / "openrouter_pricing_cache.json"
    cache.write_text(_json_mod.dumps({"acme/experimental-1": {"input": 2.0, "output": 8.0}}))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)

    def _boom():
        raise AssertionError("cache hit must not trigger a network fetch")
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", _boom)

    cost = cost_table.estimate_cost("acme/experimental-1", 1_000_000, 1_000_000)
    assert cost == 10.0


def test_openrouter_free_tier_rates_are_not_priced_as_zero(tmp_path, monkeypatch, _restore_pricing):
    """OpenRouter's `:free` tiers quote 0/0. Accepting that would log a
    confident $0.00 into daily totals; honest answer is "$?.??"."""
    from src import cost_table
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(cost_table, "_CACHE_PATH", tmp_path / "absent-litellm.json")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", lambda: None)

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": [
                {"id": "vendor/free-model:free",
                 "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "vendor/paid-model",
                 "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
            ]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    assert cost_table.estimate_cost("vendor/free-model:free", 1_000, 1_000) is None
    assert cost_table.estimate_cost("vendor/paid-model", 1_000_000, 0) == 1.0


def test_openrouter_fetch_failure_never_raises(tmp_path, monkeypatch, _restore_pricing):
    """Pricing is telemetry. An unreachable catalog must degrade to "$?.??",
    never take a trading session down."""
    from src import cost_table
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(cost_table, "_CACHE_PATH", tmp_path / "absent-litellm.json")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", lambda: None)

    import requests

    def _explode(*a, **k):
        raise requests.ConnectionError("network down")
    monkeypatch.setattr(requests, "get", _explode)

    assert cost_table.estimate_cost("vendor/whatever", 1_000, 1_000) is None
    assert fmt_cost(None) == "$?.??"


def test_openrouter_stale_cache_is_refreshed_not_trusted(tmp_path, monkeypatch, _restore_pricing):
    """A stale entry must lose to a live fetch. Serving an aged-out rate is
    how a cost report becomes confidently wrong."""
    import os as _os
    import time as _time
    from src import cost_table
    cache = tmp_path / "openrouter_pricing_cache.json"
    cache.write_text(_json_mod.dumps({"acme/drifted": {"input": 1.0, "output": 1.0}}))
    old = _time.time() - (cost_table._CACHE_MAX_AGE_SECONDS + 60)
    _os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(
        cost_table, "_fetch_openrouter_pricing",
        lambda: {"acme/drifted": {"input": 3.0, "output": 9.0}},
    )
    assert cost_table.estimate_cost("acme/drifted", 1_000_000, 0) == 3.0


def test_openrouter_stale_cache_is_last_resort_when_catalog_is_down(tmp_path, monkeypatch, _restore_pricing):
    """...but a stale rate still beats no cost at all when the catalog is
    unreachable, and it is logged as stale rather than passed off as fresh."""
    import os as _os
    import time as _time
    from src import cost_table
    cache = tmp_path / "openrouter_pricing_cache.json"
    cache.write_text(_json_mod.dumps({"acme/drifted": {"input": 1.0, "output": 1.0}}))
    old = _time.time() - (cost_table._CACHE_MAX_AGE_SECONDS + 60)
    _os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)
    assert cost_table.estimate_cost("acme/drifted", 1_000_000, 0) == 1.0


# === PR #30 blocker 3 — an OpenRouter id must never be priced by LiteLLM ===
#
# `_resolve_unknown_model` used to try OpenRouter for a "vendor/model" id and,
# on a miss, FALL THROUGH to the LiteLLM path. That path probes `<id>`,
# `openai/<id>` and `anthropic/<id>`, so a colliding row would price routed
# traffic at the vendor's DIRECT API rate. The result is not a missing number
# — it is a confidently wrong one, which is the failure this module rejects
# everywhere else (see the stale-cache tests above).


def test_openrouter_id_never_falls_through_to_a_colliding_litellm_row(
    tmp_path, monkeypatch, _restore_pricing,
):
    """THE blocker. OpenRouter's catalog is readable and does not list the
    model; LiteLLM carries a row under the very same key. The cost must stay
    unknown rather than silently become the direct-provider price."""
    from src import cost_table

    or_cache = tmp_path / "openrouter_pricing_cache.json"
    or_cache.write_text(_json_mod.dumps({"acme/something-else": {"input": 1.0, "output": 2.0}}))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", or_cache)

    # LiteLLM has an exact-key collision at a completely different rate.
    litellm_cache = tmp_path / "pricing_cache.json"
    litellm_cache.write_text(_json_mod.dumps({
        "acme/routed-model": {"input_cost_per_token": 99e-6,
                              "output_cost_per_token": 99e-6},
    }))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", litellm_cache)

    assert cost_table.estimate_cost("acme/routed-model", 1_000_000, 1_000_000) is None, (
        "an OpenRouter id OpenRouter cannot price must render '$?.??', never "
        "LiteLLM's direct-provider rate for a colliding key"
    )
    assert "acme/routed-model" not in cost_table.PRICING


def test_openrouter_id_ignores_the_provider_prefixed_litellm_probe(
    tmp_path, monkeypatch, _restore_pricing,
):
    """The subtler collision: `_litellm_entry` also probes `openai/<id>`, so
    a LiteLLM row at `openai/acme/routed-2` would have matched an id that has
    nothing to do with OpenAI."""
    from src import cost_table

    or_cache = tmp_path / "openrouter_pricing_cache.json"
    or_cache.write_text(_json_mod.dumps({"acme/other": {"input": 1.0, "output": 2.0}}))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", or_cache)

    litellm_cache = tmp_path / "pricing_cache.json"
    litellm_cache.write_text(_json_mod.dumps({
        "openai/acme/routed-2": {"input_cost_per_token": 42e-6,
                                 "output_cost_per_token": 42e-6},
    }))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", litellm_cache)

    assert cost_table.estimate_cost("acme/routed-2", 1_000_000, 1_000_000) is None


def test_openrouter_id_miss_never_reaches_the_litellm_path_at_all(
    tmp_path, monkeypatch, _restore_pricing,
):
    """Not just "the answer is None" — the LiteLLM lookup must not even run.
    A fall-through that happened to miss today would still be a live bug the
    day LiteLLM adds a colliding row."""
    from src import cost_table

    or_cache = tmp_path / "openrouter_pricing_cache.json"
    or_cache.write_text(_json_mod.dumps({"acme/other": {"input": 1.0, "output": 2.0}}))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", or_cache)

    def _boom():
        raise AssertionError("an OpenRouter id must not consult LiteLLM")
    monkeypatch.setattr(cost_table, "_read_cache_dataset", _boom)
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", _boom)

    assert cost_table.estimate_cost("acme/absent-3", 100, 50) is None


def test_openrouter_id_miss_is_memoised_when_the_catalog_was_read(
    tmp_path, monkeypatch, _restore_pricing,
):
    """A catalog we DID read and that lacks the model is a permanent answer —
    memoise it so every subsequent call is a set lookup, matching the
    `saw_dataset` discipline on the LiteLLM path."""
    from src import cost_table

    or_cache = tmp_path / "openrouter_pricing_cache.json"
    or_cache.write_text(_json_mod.dumps({"acme/other": {"input": 1.0, "output": 2.0}}))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", or_cache)

    assert cost_table.estimate_cost("acme/ghost-4", 100, 50) is None
    assert "acme/ghost-4" in cost_table._UNKNOWN_MODELS

    def _boom():
        raise AssertionError("memoised miss must not re-read the catalog")
    monkeypatch.setattr(cost_table, "_read_openrouter_cache", _boom)
    assert cost_table.estimate_cost("acme/ghost-4", 100, 50) is None


def test_openrouter_id_miss_is_not_memoised_when_the_catalog_is_unreachable(
    tmp_path, monkeypatch, _restore_pricing,
):
    """No cache and no network is a TRANSIENT failure. Memoising it would
    strand the model at '$?.??' for the life of the process even after
    connectivity returns."""
    from src import cost_table

    monkeypatch.setattr(
        cost_table, "_OPENROUTER_CACHE_PATH", tmp_path / "absent-openrouter.json",
    )
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)
    monkeypatch.setattr(cost_table, "_CACHE_PATH", tmp_path / "absent-litellm.json")
    monkeypatch.setattr(cost_table, "_fetch_litellm_dataset", lambda: None)

    assert cost_table.estimate_cost("acme/transient-5", 100, 50) is None
    assert "acme/transient-5" not in cost_table._UNKNOWN_MODELS


def test_bare_vendor_ids_still_resolve_through_litellm(tmp_path, monkeypatch, _restore_pricing):
    """The fix is scoped to ids containing '/'. A bare id — the Anthropic and
    legacy rows — must still resolve from LiteLLM exactly as before."""
    from src import cost_table

    litellm_cache = tmp_path / "pricing_cache.json"
    litellm_cache.write_text(_json_mod.dumps({
        "bare-model-6": {"input_cost_per_token": 5e-6, "output_cost_per_token": 30e-6},
    }))
    monkeypatch.setattr(cost_table, "_CACHE_PATH", litellm_cache)
    assert cost_table.estimate_cost("bare-model-6", 1_000_000, 1_000_000) == 35.0


def test_mandatory_openrouter_refresh_uses_fresh_official_cache(
    tmp_path, monkeypatch, _restore_pricing,
):
    import json
    from src import cost_table

    rates = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    rates["openai/gpt-5.5"] = {"input": 5.25, "output": 31.0}
    cache = tmp_path / "openrouter.json"
    cache.write_text(json.dumps(rates))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(
        cost_table, "_fetch_openrouter_pricing",
        lambda: (_ for _ in ()).throw(AssertionError("fresh cache must avoid network")),
    )

    assert cost_table.refresh_openrouter_pricing() is True
    assert cost_table.PRICING["openai/gpt-5.5"] == {
        "input": 5.25, "output": 31.0,
    }


def test_mandatory_openrouter_refresh_rejects_stale_cache_when_network_is_down(
    tmp_path, monkeypatch, _restore_pricing,
):
    import json
    import os
    import time
    from src import cost_table

    cache = tmp_path / "openrouter.json"
    cache.write_text(json.dumps(cost_table._PRICING_OPENROUTER))
    old = time.time() - cost_table._CACHE_MAX_AGE_SECONDS - 60
    os.utime(cache, (old, old))
    monkeypatch.setattr(cost_table, "_OPENROUTER_CACHE_PATH", cache)
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: None)

    assert cost_table.refresh_openrouter_pricing() is False


def test_mandatory_openrouter_refresh_applies_live_catalog_rates(
    tmp_path, monkeypatch, _restore_pricing,
):
    from src import cost_table

    live = {model: dict(value) for model, value in cost_table._PRICING_OPENROUTER.items()}
    live["google/gemini-2.5-flash-lite"] = {"input": 0.11, "output": 0.44}
    monkeypatch.setattr(
        cost_table, "_OPENROUTER_CACHE_PATH", tmp_path / "absent.json",
    )
    monkeypatch.setattr(cost_table, "_fetch_openrouter_pricing", lambda: live)

    assert cost_table.refresh_openrouter_pricing(force=True) is True
    assert cost_table.PRICING["google/gemini-2.5-flash-lite"] == {
        "input": 0.11, "output": 0.44,
    }
