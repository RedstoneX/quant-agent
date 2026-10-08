"""Boundary witness for the Sentinel outward seam: builds, signs, verifies and drops with no pipeline.

Fixtures are synthetic (placeholder symbols, round numbers, a literal
test key) because the repository is public.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sentinel_seam import snapshot as snap
from src.sentinel_seam.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    SIGNING_KEY_ENV_VAR,
    SnapshotPublisher,
    build_snapshot,
    sign_snapshot,
    signing_key_from_environ,
    verify_snapshot,
)
from tests.boundary_harness import check_boundary

KEY = b"test-only-key-not-a-secret"
NOW = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
EXPECTED_SECTIONS = {
    "schema_version", "heartbeat_at", "desk_version", "trading_state",
    "expected_positions", "expected_protections", "risk_state",
    "last_reconciliation", "recent_trades", "cost_spent", "signature",
}


def _state() -> dict:
    return {
        "trading_state": {"mode": "paper", "halted": False},
        "expected_positions": [{"symbol": "TEST1", "qty": 10, "side": "long"}],
        "expected_protections": [{"symbol": "TEST1", "stop_order_id": "abc", "stop_price": 1.0}],
        "risk_state": {"open_risk_pct": 0.0},
        "last_reconciliation": {"at": NOW.isoformat(), "clean": True},
    }


def _payload() -> dict:
    return build_snapshot(heartbeat_at=NOW, desk_version="v-test", **_state())


# -- boundary clauses -------------------------------------------------------

def test_module_passes_the_boundary_harness():
    verdict = check_boundary("src.sentinel_seam.snapshot")
    assert not verdict.failures, verdict.failures


def test_constructor_takes_only_keyword_collaborators_and_builds_alone(tmp_path):
    params = inspect.signature(SnapshotPublisher).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    SnapshotPublisher(state_reader=_state, output_path=tmp_path / "s.json",
                      desk_version="v", signing_key=None)


def test_module_imports_nothing_from_src():
    text = Path(snap.__file__).read_text(encoding="utf-8")
    assert "from src" not in text and "import src" not in text


# -- payload shape -----------------------------------------------------------

def test_payload_shape_carries_every_spec_section_and_the_schema_version():
    env = sign_snapshot(_payload(), key=KEY)
    assert set(env) == EXPECTED_SECTIONS
    assert env["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert env["heartbeat_at"] == "2026-01-02T14:30:00+00:00"
    assert env["recent_trades"] == [] and env["cost_spent"] == {}
    json.dumps(env)  # serialisable as-is


def test_naive_heartbeat_is_refused():
    with pytest.raises(ValueError):
        build_snapshot(heartbeat_at=NOW.replace(tzinfo=None), desk_version="v", **_state())


# -- signing -----------------------------------------------------------------

def test_signature_verifies_with_the_key_and_not_with_another():
    env = sign_snapshot(_payload(), key=KEY)
    assert env["signature"]["scheme"] == "hmac-sha256"
    assert verify_snapshot(env, key=KEY)
    assert not verify_snapshot(env, key=b"some-other-key")


def test_signature_survives_a_json_round_trip_as_another_host_would_read_it():
    env = sign_snapshot(_payload(), key=KEY)
    assert verify_snapshot(json.loads(json.dumps(env)), key=KEY)


def test_tampered_payload_fails_verification():
    env = sign_snapshot(_payload(), key=KEY)
    env["expected_positions"][0]["qty"] = 11
    assert not verify_snapshot(env, key=KEY)
    env2 = sign_snapshot(_payload(), key=KEY)
    env2["signature"]["value"] = "0" * 64
    assert not verify_snapshot(env2, key=KEY)


def test_unsigned_case_is_explicit_and_never_verifies():
    env = sign_snapshot(_payload(), key=None)
    assert env["signature"] == {"scheme": "unsigned", "value": None}
    assert not verify_snapshot(env, key=KEY)


def test_signing_key_comes_from_environment_or_is_absent():
    assert signing_key_from_environ({}) is None
    assert signing_key_from_environ({SIGNING_KEY_ENV_VAR: "  "}) is None
    assert signing_key_from_environ({SIGNING_KEY_ENV_VAR: "k\n"}) == b"k"


# -- publisher ---------------------------------------------------------------

def test_publisher_drops_a_verifiable_file_atomically(tmp_path):
    out = tmp_path / "drop" / "snapshot.json"
    pub = SnapshotPublisher(state_reader=_state, output_path=out, desk_version="v-test",
                            signing_key=KEY, clock=lambda: NOW)
    written = pub.publish()
    assert pub.is_signed
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk == written
    assert verify_snapshot(on_disk, key=KEY)
    assert not out.with_name("snapshot.json.tmp").exists()


def test_publisher_without_key_writes_an_unsigned_snapshot(tmp_path):
    out = tmp_path / "snapshot.json"
    pub = SnapshotPublisher(state_reader=_state, output_path=out, desk_version="v",
                            signing_key=None, clock=lambda: NOW)
    assert not pub.is_signed
    assert pub.publish()["signature"]["scheme"] == "unsigned"


# -- scrubber (docs/FUTURE.md: "a scrubber nobody checks is a scrubber that rots") --

from src.sentinel_seam.snapshot import UNKNOWN_VERSION, desk_code_version, scrub_snapshot  # noqa: E402

LEAKY = {
    "trading_state": {
        "account_id": "11111111-2222-3333-4444-555555555555",   # by key name
        "note": "acct PA00000000 rehearsal",                   # account number by shape
        "api_key": "whatever",                                   # by key name
        "log": "sent Bearer abcdefghijklmnop to venue",          # bearer token by shape
        "where": "wal at /home/qamc/data/desk.db",               # filesystem path
        "key_like": "PKTESTTESTTESTTEST12",                      # provider key prefix
        "blob": "A" * 40,                                        # long opaque secret
        "host": "desk-box.internal",                             # by key name
        "peer": "reaches qamc-box.internal nightly",             # hostname by shape
        "owner": "someone@example.com",                          # e-mail by shape
        "ip": "connected from 10.0.0.7",                         # IPv4 by shape
        "mode": "paper",                                         # must survive
    },
    "expected_positions": [{"symbol": "TEST1", "qty": 10}],
    "nested": [[{"secret_token": "x"}]],
}


def test_scrubber_redacts_every_identifying_field_and_keeps_the_rest():
    out = scrub_snapshot(LEAKY)
    flat = json.dumps(out)
    for leak in ("11111111-2222", "PA00000000", "Bearer abcdefghijklmnop", "/home/qamc",
                 "PKTESTTESTTESTTEST12", "A" * 40, "desk-box.internal", "qamc-box.internal",
                 "someone@example.com", "10.0.0.7"):
        assert leak not in flat, leak
    ts = out["trading_state"]
    assert ts["account_id"] == ts["api_key"] == "[REDACTED]" and ts["host"] == "[HOST REDACTED]"
    assert "[ACCOUNT REDACTED]" in ts["note"] and "[PATH REDACTED]" in ts["where"]
    assert "[HOST REDACTED]" in ts["peer"] and ts["mode"] == "paper"
    assert out["nested"][0][0]["secret_token"] == "[REDACTED]"
    assert out["expected_positions"] == [{"symbol": "TEST1", "qty": 10}]
    assert LEAKY["trading_state"]["mode"] == "paper" and "api_key" in LEAKY["trading_state"]  # pure


def test_publisher_scrubs_before_it_signs(tmp_path):
    def leaky_state():
        s = _state()
        s["trading_state"]["api_key"] = "x"
        return s
    pub = SnapshotPublisher(state_reader=leaky_state, output_path=tmp_path / "s.json",
                            desk_version="v", signing_key=KEY, clock=lambda: NOW)
    env = pub.publish()
    assert env["trading_state"]["api_key"] == "[REDACTED]"
    assert verify_snapshot(env, key=KEY)   # the seal covers the scrubbed body


# -- code version ------------------------------------------------------------

class _Run:
    def __init__(self, stdout="", rc=0): self.stdout, self.returncode = stdout, rc


def test_code_version_prefers_git_then_falls_back_to_the_explicit_unknown(monkeypatch):
    assert desk_code_version(run=lambda *a, **k: _Run("abc1234\n")) == "abc1234"
    from importlib import metadata
    monkeypatch.setattr(metadata, "version", lambda name: (_ for _ in ()).throw(metadata.PackageNotFoundError(name)))
    assert desk_code_version(run=lambda *a, **k: _Run("", rc=128)) == UNKNOWN_VERSION
    def boom(*a, **k): raise OSError("no git")
    assert desk_code_version(run=boom) == UNKNOWN_VERSION


def test_code_version_against_the_real_checkout_is_a_short_sha_or_unknown():
    v = desk_code_version()
    assert v == UNKNOWN_VERSION or len(v) >= 7
