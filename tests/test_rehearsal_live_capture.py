"""The live capture boundary must not inherit a desk broker credential."""

import os
from types import SimpleNamespace

import pytest
import requests

from ops.rehearsal.live_capture import (
    LiveCaptureError,
    _assert_disk_headroom,
    _isolate_broker_environment,
)


def test_broker_environment_bypasses_only_alpaca_without_losing_onecli(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "inherited-desk-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "inherited-desk-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://onecli.invalid:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/onecli/private-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/onecli/private-ca.pem")
    monkeypatch.setenv("NO_PROXY", "localhost,internal.example")
    monkeypatch.delenv("no_proxy", raising=False)

    _isolate_broker_environment()

    assert "ALPACA_API_KEY" not in os.environ
    assert "ALPACA_SECRET_KEY" not in os.environ
    assert "REQUESTS_CA_BUNDLE" not in os.environ
    assert os.environ["SSL_CERT_FILE"] == "/onecli/private-ca.pem"
    assert os.environ["HTTPS_PROXY"] == "http://onecli.invalid:8080"
    assert requests.utils.should_bypass_proxies("https://paper-api.alpaca.markets/v2/account", no_proxy=None)
    assert requests.utils.should_bypass_proxies("https://data.alpaca.markets/v2/stocks/bars", no_proxy=None)
    assert not requests.utils.should_bypass_proxies("https://api.openai.com/v1/responses", no_proxy=None)
    assert "internal.example" in os.environ["NO_PROXY"]


def test_capture_refuses_insufficient_disk_headroom(monkeypatch, tmp_path):
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=7,
            f_frsize=1,
        ),
    )
    with pytest.raises(LiveCaptureError, match="disk headroom"):
        _assert_disk_headroom(tmp_path, 1)
