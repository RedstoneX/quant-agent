"""Board item 202: no new outbound-client site in ``src/`` without a replay seam.

A replay of a recorded session must be deterministic and free. The runtime wall
catches an outbound attempt when it happens; this guard catches it when it is
written. The scanner and the policy list of client modules live in
``scripts/replay_outbound_guard.py``.

NO STORED BASELINE
------------------
An earlier cut of this guard read ``tests/replay_outbound_sites_baseline.json``
-- an AST scan of ``src/`` frozen on one day and committed. That was a cached
measurement, so it collided with every other open change that touched a listed
file. The guard now stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md): it
scans this working tree, scans ``origin/main`` again at check time, and fails
only on the DELTA. If ``origin/main`` cannot be read it REFUSES.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import guard_reference, replay_outbound_guard
from scripts.guard_reference import ReferenceUnavailable


def test_no_new_outbound_client_site_without_a_replay_seam():
    bad = replay_outbound_guard.violations()
    assert not bad, (
        "NEW outbound-client import(s) in src/ that a replay has no named seam for. "
        "A replay must be served from a recording or refuse naming the missing "
        "recording, never reach a live provider (board item 202). Handle it in "
        "ops/rehearsal first:\n  " + "\n  ".join(bad)
    )


def test_the_trunk_is_actually_measured():
    """A guard that measured nothing on the trunk would pass vacuously."""
    paths = replay_outbound_guard.scanned_paths()
    assert len(paths) > 50, f"only {len(paths)} scanned src/*.py files seen"
    before = replay_outbound_guard.trunk_sites(paths)
    assert before, "no outbound site measured on origin/main -- the reference read is dead"


def test_a_new_site_is_caught_and_reported_as_a_delta(monkeypatch):
    """Pretend the trunk copy lacked one client: the guard must report the delta."""
    now = replay_outbound_guard.working_sites()
    assert now, "nothing to compare against"
    path = sorted(now)[0]
    dropped = sorted(now[path])[0]
    real = replay_outbound_guard.trunk_sites

    def without(paths):
        before = real(paths)
        before[path] = before.get(path, set()) - {dropped}
        return before

    monkeypatch.setattr(replay_outbound_guard, "trunk_sites", without)
    bad = replay_outbound_guard.violations()
    assert any(path in line and dropped in line and "+[" in line for line in bad), bad


def test_dropping_one_client_and_adding_another_still_fails(monkeypatch):
    """-1 old +1 new in the same file nets to zero clients; identity must still fail."""
    now = replay_outbound_guard.working_sites()
    path = sorted(now)[0]
    swapped = dict(now)
    swapped[path] = (now[path] - {sorted(now[path])[0]}) | {"smtplib"}
    monkeypatch.setattr(replay_outbound_guard, "working_sites", lambda: swapped)
    bad = replay_outbound_guard.violations()
    assert len(bad) == 1 and path in bad[0] and "smtplib" in bad[0], bad


def test_it_refuses_when_the_trunk_cannot_be_read(tmp_path, monkeypatch):
    repo = tmp_path / "norepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "m.py").write_text("import requests\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "add", "src/m.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "base"], cwd=repo, check=True,
    )
    monkeypatch.setattr(guard_reference, "ROOT", Path(repo))
    monkeypatch.setattr(replay_outbound_guard, "ROOT", Path(repo))

    with pytest.raises(ReferenceUnavailable) as exc:
        replay_outbound_guard.violations()
    assert "origin/main" in str(exc.value)
    assert replay_outbound_guard.main() == 2


def test_the_scanner_sees_each_import_shape():
    assert replay_outbound_guard.scan_text("import requests\n") == {"requests"}
    assert replay_outbound_guard.scan_text("import urllib.request\n") == {"urllib"}
    assert replay_outbound_guard.scan_text("from httpx import Client\n") == {"httpx"}
    assert replay_outbound_guard.scan_text(
        "def f():\n    import yfinance\n") == {"yfinance"}
    assert replay_outbound_guard.scan_text("from . import socket\n") == set()
    assert replay_outbound_guard.scan_text("import json\nimport pandas\n") == set()


# --- the guard's SUBJECT is named by identity: what is a client, what is not ---

EXCLUDED_IMPORTS = {
    "from urllib.parse import urlsplit\n": "urllib.parse",
    "import urllib.parse\n": "urllib.parse",
    "from alpaca.trading.requests import GetOrdersRequest\n": "alpaca.trading.requests",
    "from alpaca.trading.enums import OrderSide\n": "alpaca.trading.enums",
}


@pytest.mark.parametrize("source,submodule", sorted(EXCLUDED_IMPORTS.items()))
def test_each_excluded_submodule_is_excluded_for_its_stated_reason(source, submodule):
    reason = replay_outbound_guard.NOT_CLIENT_SUBMODULES[submodule]
    assert reason.strip(), f"{submodule} is excluded with no reason"
    assert replay_outbound_guard.scan_text(source) == set()


def test_the_exclusion_list_is_exactly_these_names():
    """Identity, not a count: a new entry must be named here with its reason."""
    assert set(replay_outbound_guard.NOT_CLIENT_SUBMODULES) == {
        "urllib.parse", "alpaca.trading.requests", "alpaca.trading.enums"}
    assert set(replay_outbound_guard.NOT_CLIENT_ATTRIBUTES["socket"]) == {
        "setdefaulttimeout", "getdefaulttimeout"}


@pytest.mark.parametrize("source,client", [
    ("import urllib.request\n", "urllib"),
    ("from urllib.request import urlopen\n", "urllib"),
    ("from urllib.parse import urlsplit\nimport urllib.request\n", "urllib"),
    ("from alpaca.trading.client import TradingClient\n", "alpaca"),
    ("from alpaca.trading.stream import TradingStream\n", "alpaca"),
    ("from alpaca.data.historical.stock import StockHistoricalDataClient\n", "alpaca"),
    ("from alpaca.trading.requests import GetOrdersRequest\n"
     "from alpaca.trading.client import TradingClient\n", "alpaca"),
    ("import socket\nsocket.create_connection(('h', 1))\n", "socket"),
    ("import socket\nsocket.setdefaulttimeout(3)\nsocket.socket()\n", "socket"),
    ("from socket import create_connection\n", "socket"),
])
def test_a_genuinely_outbound_import_is_still_flagged(source, client):
    assert replay_outbound_guard.scan_text(source) == {client}


def test_socket_timeout_helpers_alone_are_not_a_client():
    src = "import socket\nold = socket.getdefaulttimeout()\nsocket.setdefaulttimeout(old)\n"
    assert replay_outbound_guard.scan_text(src) == set()


def test_a_new_outbound_import_in_a_cleared_file_is_refused(monkeypatch):
    """The failing case: a file with only a request model gains a real client."""
    path = "src/execution/stop_read.py"
    assert path not in replay_outbound_guard.working_sites()
    grown = dict(replay_outbound_guard.working_sites())
    grown[path] = replay_outbound_guard.scan_text(
        "from alpaca.trading.requests import GetOrdersRequest\n"
        "from alpaca.trading.client import TradingClient\n")
    monkeypatch.setattr(replay_outbound_guard, "working_sites", lambda: grown)
    bad = replay_outbound_guard.violations()
    assert len(bad) == 1 and path in bad[0] and "alpaca" in bad[0], bad
