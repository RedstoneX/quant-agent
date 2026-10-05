from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.rehearsal import public_bundle
from ops.rehearsal.public_bundle import (
    PublicBundleRejected,
    assert_public_safe,
    promote_public_bundle,
)


SAFE_PAYLOAD = {
    "schema": "qamc-broker-cassette-v1",
    "entries": [
        {
            "client": "trading",
            "method": "get_account",
            "answer": {
                "account_number": "<QAMC:account:0001>",
                "id": "<QAMC:account_id:0001>",
            },
        }
    ],
}


@pytest.mark.parametrize(
    "unsafe",
    [
        {"value": "exact-secondary-secret"},
        {"value": "prefix-exact\nsecondary-secret-suffix"},
        {"value": "PASECONDARY123456"},
        {"Authorization": "Bearer anything"},
        {"url": "https://broker.invalid/v2?api_key=credential"},
        {"value": "00000000-0000-0000-0000-000000000000"},
        {"value": "/home/qamc/quant-agent/data/quant_agent.db"},
        {"id": 987654321012345},
        {"broker_order_id": "raw-broker-identifier"},
        {"broker_execution_id": "raw-broker-execution-identifier"},
    ],
)
def test_public_safety_gate_rejects_every_private_class_without_echoing_it(unsafe):
    with pytest.raises(PublicBundleRejected) as error:
        assert_public_safe(
            unsafe,
            secrets=("exact-secondary-secret", "exact\nsecondary-secret"),
            account_ids=("PASECONDARY123456",),
        )
    message = str(error.value)
    assert "exact-secondary-secret" not in message
    assert "PASECONDARY123456" not in message


def test_public_safety_gate_accepts_only_tokenized_identifier_fields():
    encoded = assert_public_safe(SAFE_PAYLOAD)
    assert json.loads(encoded) == SAFE_PAYLOAD


def test_public_gate_preserves_non_broker_replay_identifiers():
    payload = {
        **SAFE_PAYLOAD,
        "replay_metadata": {
            "run_id": "morning-2026-10-05-test",
            "decision_id": "pm-refusal-7",
            "session_id": "bounded-secondary-capture",
        },
    }

    encoded = assert_public_safe(payload)
    assert json.loads(encoded)["replay_metadata"] == payload["replay_metadata"]


def test_capture_staging_path_is_gitignored():
    repository_root = Path(__file__).resolve().parents[1]
    ignored = (repository_root / ".gitignore").read_text()
    assert "/ops/rehearsal/captures.local/" in ignored


def test_promotion_writes_ignored_staging_first_then_atomically_moves(
    monkeypatch, tmp_path
):
    root = tmp_path / "repo"
    staging = root / "ops" / "rehearsal" / "captures.local"
    destination = root / "ops" / "rehearsal" / "recordings" / "broker.json"
    observed_staged_file: list[Path] = []
    real_assert = public_bundle.assert_public_safe

    def asserting_after_stage(payload, **kwargs):
        observed_staged_file.extend(staging.glob("*.json"))
        assert observed_staged_file
        return real_assert(payload, **kwargs)

    monkeypatch.setattr(public_bundle, "assert_public_safe", asserting_after_stage)
    promoted = promote_public_bundle(
        SAFE_PAYLOAD,
        destination,
        repository_root=root,
        staging_dir=staging,
    )

    assert promoted == destination
    assert json.loads(destination.read_text()) == SAFE_PAYLOAD
    assert not list(staging.iterdir())


def test_failed_scan_removes_staged_file_and_never_creates_destination(tmp_path):
    root = tmp_path / "repo"
    staging = root / "ops" / "rehearsal" / "captures.local"
    destination = root / "ops" / "rehearsal" / "recordings" / "broker.json"

    with pytest.raises(PublicBundleRejected):
        promote_public_bundle(
            {"secret": "exact-secondary-secret"},
            destination,
            repository_root=root,
            staging_dir=staging,
            secrets=("exact-secondary-secret",),
        )

    assert not destination.exists()
    assert staging.exists()
    assert not list(staging.iterdir())
