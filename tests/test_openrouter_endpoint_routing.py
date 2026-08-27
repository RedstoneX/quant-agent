"""OpenRouter endpoint (provider) routing and provider-reported cost.

OpenRouter serves one model id from several endpoints at DIFFERENT PRICES.
`openai/gpt-5.5` is offered by `openai/flex` at $2.50/$15 per M tokens and by
`openai` / `azure` at $5/$30 — the same `gpt-5.5-20260423` weights either way.
Two consequences, and this module covers both:

  1. A seat must be able to state an endpoint preference without that being a
     model change (no benchmark, no routing-policy decision).
  2. Once a model's price depends on which endpoint served it, a cost table
     keyed on the model id alone cannot be right. The daily cost circuit
     spends against these numbers, so the call must be priced from what
     OpenRouter says it charged.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import _reported_cost_usd
from tests.test_base_agent import ConcreteAgent, _openai_stream_mock, _stream_chunk


def _usage(prompt=100, completion=50, cost="unset"):
    """Usage chunk with an explicitly controlled `cost`.

    `_stream_usage` in test_base_agent deliberately leaves `cost` as a
    MagicMock auto-attribute; here we set it (or delete it) on purpose.
    """
    u = MagicMock()
    u.prompt_tokens = prompt
    u.completion_tokens = completion
    if cost == "unset":
        del u.cost
    else:
        u.cost = cost
    return u


def _stream_mock_with_usage(usage):
    oai = MagicMock()
    oai.chat.completions.create.return_value = [
        _stream_chunk(piece='{"result": "ok"}'),
        _stream_chunk(finish_reason="stop"),
        _stream_chunk(usage=usage),
    ]
    return oai


def _openrouter_agent(client, **kwargs):
    return ConcreteAgent(
        api_key="k", model="openai/gpt-5.5", max_tokens=4096,
        provider="openrouter", **kwargs,
    )


# --------------------------------------------------------------------------
# Endpoint preference reaches the wire
# --------------------------------------------------------------------------

def test_provider_order_is_sent_as_openrouter_provider_routing():
    """The preference must arrive as OpenRouter's `provider.order`, with
    fallbacks ENABLED. `only` would fail the seat closed over a price tier —
    losing a trading session to save a fraction of a cent."""
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = _openrouter_agent(client, provider_order=["openai/flex"])
        agent.run(data="x")
        body = client.chat.completions.create.call_args.kwargs["extra_body"]
    assert body["provider"] == {"order": ["openai/flex"], "allow_fallbacks": True}


def test_provider_order_does_not_change_the_requested_model():
    """An endpoint preference selects WHO serves the seat's model, never WHICH
    model. If this ever drifts it becomes an unrecorded model choice, which is
    exactly what MODEL_ROUTING_POLICY.md forbids."""
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = _openrouter_agent(client, provider_order=["openai/flex"])
        result = agent.run(data="x")
        assert client.chat.completions.create.call_args.kwargs["model"] == "openai/gpt-5.5"
    assert result.model == "openai/gpt-5.5"
    assert result.requested_model == "openai/gpt-5.5"


def test_openrouter_always_requests_usage_accounting():
    """Even with no endpoint preference, OpenRouter traffic must ask for the
    billed cost — that is what makes the figure available at all."""
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = _openrouter_agent(client)
        agent.run(data="x")
        body = client.chat.completions.create.call_args.kwargs["extra_body"]
    assert body["usage"] == {"include": True}
    assert "provider" not in body


def test_plain_openai_sends_no_extra_body():
    """`usage.include` and `provider.order` are OpenRouter extensions. Sending
    them to OpenAI (or the relay in front of it) risks a 400 on a request that
    would otherwise have succeeded."""
    with patch("openai.OpenAI") as oai_cls:
        client = _openai_stream_mock()
        oai_cls.return_value = client
        agent = ConcreteAgent(api_key="k", model="gpt-5.5", max_tokens=4096)
        agent.run(data="x")
        assert "extra_body" not in client.chat.completions.create.call_args.kwargs


# --------------------------------------------------------------------------
# Provider-reported cost
# --------------------------------------------------------------------------

def test_reported_cost_beats_the_pinned_estimate():
    """The pinned table prices `openai/gpt-5.5` at the full $5/$30 endpoint.
    Served by flex the call really costs half that, and the recorded figure
    must be the invoice, not the table — otherwise the daily circuit charges
    the budget twice what the run actually spent."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _stream_mock_with_usage(_usage(cost=0.000375))
        agent = _openrouter_agent(None, provider_order=["openai/flex"])
        result = agent.run(data="x")
    # 100 in / 50 out at the pinned $5/$30 = $0.00050 + $0.00150 = $0.002.
    # Flex charged $0.000375. The estimate must not be what was recorded.
    assert result.cost_usd == pytest.approx(0.000375)


def test_absent_reported_cost_falls_back_to_the_estimate():
    """A provider that does not report cost must leave the existing
    pinned-rate behavior exactly as it was."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _stream_mock_with_usage(_usage(cost="unset"))
        agent = _openrouter_agent(None)
        result = agent.run(data="x")
    assert result.cost_usd == pytest.approx(0.002)


def test_zero_reported_cost_is_recorded_not_discarded():
    """$0.00 is a legitimate charge (a free-tier endpoint) and must survive.
    Treating it as falsy would silently re-price the call at the pinned rate."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _stream_mock_with_usage(_usage(cost=0.0))
        agent = _openrouter_agent(None)
        result = agent.run(data="x")
    assert result.cost_usd == 0.0


def test_zero_tokens_still_reports_unknown_cost():
    """The pre-existing 'no usage data' contract wins over a reported cost:
    zero tokens means the response carried no telemetry, which the operator
    must investigate rather than see summed into a daily total."""
    with patch("openai.OpenAI") as oai_cls:
        oai_cls.return_value = _stream_mock_with_usage(
            _usage(prompt=0, completion=0, cost=0.5)
        )
        agent = _openrouter_agent(None)
        result = agent.run(data="x")
    assert result.cost_usd is None


@pytest.mark.parametrize("bad", [
    True,                    # bool is an int subclass — True would price at $1
    "0.002",                 # a string rate is not a number
    float("nan"),
    float("inf"),
    -0.001,                  # a negative charge would REFUND the daily budget
    None,
])
def test_unusable_reported_cost_degrades_to_the_estimate(bad):
    """Under-reporting is the dangerous direction: the daily cost circuit
    spends against this number, so anything that is not a finite non-negative
    real must leave the pinned estimate standing."""
    assert _reported_cost_usd(_usage(cost=bad), "test_agent") is None


def test_magicmock_usage_attribute_does_not_become_a_cost():
    """A MagicMock auto-attribute is the exact hazard `_coerce_token_count`
    was written for — it must not be mistaken for a billed amount."""
    u = MagicMock()
    u.prompt_tokens, u.completion_tokens = 100, 50
    assert _reported_cost_usd(u, "test_agent") is None
    assert _reported_cost_usd(None, "test_agent") is None
