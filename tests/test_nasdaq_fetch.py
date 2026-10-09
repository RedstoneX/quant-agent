"""nasdaq_fetch.fetch_json: hermetic, requests.get is replaced."""

from unittest import mock

import pytest
import requests

from src.data import nasdaq_fetch


def test_fetch_json_returns_the_body_with_a_timeout():
    resp = mock.Mock()
    resp.json.return_value = {"data": 1}
    with mock.patch.object(nasdaq_fetch.requests, "get", return_value=resp) as get:
        assert nasdaq_fetch.fetch_json("http://x", {"A": "b"}) == {"data": 1}
    assert get.call_args.args == ("http://x",)
    assert get.call_args.kwargs["headers"] == {"A": "b"} and get.call_args.kwargs["timeout"] > 0
    resp.raise_for_status.assert_called_once()


def test_fetch_json_raises_on_http_error():
    resp = mock.Mock()
    resp.raise_for_status.side_effect = requests.HTTPError("429")
    with mock.patch.object(nasdaq_fetch.requests, "get", return_value=resp):
        with pytest.raises(requests.HTTPError):
            nasdaq_fetch.fetch_json("http://x", {})
