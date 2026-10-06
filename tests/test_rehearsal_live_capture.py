"""The live capture boundary must not inherit a desk broker credential."""

import os

import requests

from ops.rehearsal.live_capture import _isolate_broker_environment


def test_broker_environment_bypasses_only_alpaca_without_losing_onecli(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "inherited-desk-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "inherited-desk-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://onecli.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,internal.example")
    monkeypatch.delenv("no_proxy", raising=False)

    _isolate_broker_environment()

    assert "ALPACA_API_KEY" not in os.environ
    assert "ALPACA_SECRET_KEY" not in os.environ
    assert os.environ["HTTPS_PROXY"] == "http://onecli.invalid:8080"
    assert requests.utils.should_bypass_proxies(
        "https://paper-api.alpaca.markets/v2/account", no_proxy=None
    )
    assert requests.utils.should_bypass_proxies(
        "https://data.alpaca.markets/v2/stocks/bars", no_proxy=None
    )
    assert not requests.utils.should_bypass_proxies(
        "https://api.openai.com/v1/responses", no_proxy=None
    )
    assert "internal.example" in os.environ["NO_PROXY"]
