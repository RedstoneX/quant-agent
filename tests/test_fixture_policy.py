"""ops/model_policy/fixture_policy.py: the raw-facts-only exam rule.

Offline: no network, no fixtures-dir mutation — every check builds its own
manifests under `tmp_path` so this suite can never corrupt a real fixture.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from ops.model_policy import fixture_policy as fp

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _ok_manifest(**extra) -> dict:
    base = {
        "filing": {"symbol": "MRVL", "cik": "1835632"},
        "_provenance": {
            "sections": {
                "filing": {
                    "source": "https://data.sec.gov/submissions/CIK0001835632.json",
                    "fetched_on": "2026-09-14",
                    "fetch": "EarningsDataProvider._get_cik",
                },
            },
        },
    }
    base.update(extra)
    return base


# --- admissible baseline ----------------------------------------------------

def test_clean_manifest_is_admissible(tmp_path):
    path = _write(tmp_path, "ok.json", _ok_manifest())
    verdict = fp.check_fixture(path)
    assert verdict.admissible, verdict.problems


# --- rule (a): every data section needs source + fetched_on + fetch --------

def test_missing_provenance_block_is_quarantined(tmp_path):
    path = _write(tmp_path, "bad.json", {"filing": {"symbol": "MRVL"}})
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("_provenance" in p for p in verdict.problems)


def test_section_with_no_provenance_entry_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["extra_section"] = {"x": 1}
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("extra_section" in p and "no provenance entry" in p for p in verdict.problems)


def test_source_not_in_allowlist_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["_provenance"]["sections"]["filing"]["source"] = "https://example.com/not-real"
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("no external source" in p for p in verdict.problems)


def test_bad_fetched_on_date_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["_provenance"]["sections"]["filing"]["fetched_on"] = "not-a-date"
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("ISO `fetched_on`" in p for p in verdict.problems)


def test_missing_fetch_function_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["_provenance"]["sections"]["filing"]["fetch"] = ""
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("does not name the fetch function" in p for p in verdict.problems)


# --- rule (b): desk provenance / trust cut-off ------------------------------

def test_desk_source_marker_is_quarantined_while_cutoff_unset(tmp_path):
    assert fp.DESK_DATA_TRUSTED_FROM is None  # the guard this test protects
    manifest = _ok_manifest()
    manifest["filing"]["note"] = "read from /home/qamc/data/earnings/cache.json"
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("names desk records as a source" in p for p in verdict.problems)
    assert "DESK_DATA_TRUSTED_FROM is unset" in verdict.reason()


def test_desk_run_id_pattern_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["filing"]["run_ref"] = "close-1a2b3c4d"
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("names desk records as a source" in p for p in verdict.problems)


def test_desk_rows_trusted_requires_cutoff_and_dated_rows():
    # cutoff unset -> never trusted, whatever the provenance claims
    trusted, why = fp._desk_rows_trusted({"desk_rows": [{"dated": "2026-01-01"}]})
    assert trusted is False and "unset" in why


def test_changing_the_trust_cutoff_needs_an_incident_history_justification():
    """Owner rule (fixture_policy.py docstring): setting DESK_DATA_TRUSTED_FROM
    is a reviewed change that must be justified in docs/INCIDENT_HISTORY.md,
    naming the setting and the exact date. This guards that promise: as long
    as the cutoff stays unset there is nothing to justify; the moment someone
    sets it, this test starts requiring the justification text to exist.
    """
    incident_history = (PROJECT_ROOT / "docs" / "INCIDENT_HISTORY.md").read_text()
    if fp.DESK_DATA_TRUSTED_FROM is None:
        pytest.skip("DESK_DATA_TRUSTED_FROM is unset — nothing to justify yet")
    assert "DESK_DATA_TRUSTED_FROM" in incident_history
    assert fp.DESK_DATA_TRUSTED_FROM in incident_history


# --- rule (c): agent-output / old-code-derived keys -------------------------

def test_agent_output_key_anywhere_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["filing"]["prior_ratings"] = {"AAPL": {"rating": "buy"}}
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("agent-output field" in p for p in verdict.problems)


def test_old_code_derived_key_anywhere_is_quarantined(tmp_path):
    manifest = _ok_manifest()
    manifest["filing"]["indicators"] = {"rsi_14": 55}
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("derived field" in p for p in verdict.problems)


def test_nested_forbidden_key_is_caught(tmp_path):
    manifest = _ok_manifest()
    manifest["filing"]["nested"] = {"deep": {"macro_summary": {}}}
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("macro_summary" in p for p in verdict.problems)


# --- blobs -------------------------------------------------------------

def test_blob_sha256_mismatch_is_quarantined(tmp_path):
    blob_bytes = b"raw filing bytes"
    (tmp_path / "blob.bin").write_bytes(blob_bytes)
    manifest = _ok_manifest(_blobs={
        "blob.bin": {"sha256": "0" * 64, "source": "yfinance"},
    })
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("does not match its recorded sha256" in p for p in verdict.problems)


def test_missing_blob_is_quarantined(tmp_path):
    manifest = _ok_manifest(_blobs={
        "missing.bin": {"sha256": "0" * 64, "source": "yfinance"},
    })
    path = _write(tmp_path, "bad.json", manifest)
    verdict = fp.check_fixture(path)
    assert not verdict.admissible
    assert any("is missing" in p for p in verdict.problems)


def test_blob_with_valid_hash_and_source_admits(tmp_path):
    blob_bytes = gzip.compress(b"raw filing bytes")
    (tmp_path / "blob.bin.gz").write_bytes(blob_bytes)
    sha = hashlib.sha256(blob_bytes).hexdigest()
    manifest = _ok_manifest(_blobs={
        "blob.bin.gz": {"sha256": sha, "source": "yfinance"},
    })
    path = _write(tmp_path, "ok.json", manifest)
    verdict = fp.check_fixture(path)
    assert verdict.admissible, verdict.problems
    assert fp.load_blob("ok.json", "blob.bin.gz", directory=tmp_path) == b"raw filing bytes"


# --- check_all: quarantine list + stray files -------------------------------

def test_check_all_flags_stray_unowned_files(tmp_path):
    _write(tmp_path, "ok.json", _ok_manifest())
    (tmp_path / "orphan.bin").write_bytes(b"nobody pins me")
    verdicts = fp.check_all(tmp_path)
    assert verdicts["ok.json"].admissible
    assert not verdicts["orphan.bin"].admissible
    assert "no manifest pins it as a blob" in verdicts["orphan.bin"].problems[0]


def test_check_all_over_real_fixtures_dir_names_every_problem_fixture():
    """The full quarantine list this repo currently ships, as a single
    named inventory — a reviewer can read this test's failure to see
    exactly which fixture is quarantined and why, without re-deriving it."""
    verdicts = fp.check_all()
    quarantined = {name: v.reason() for name, v in verdicts.items() if not v.admissible}
    # The two desk-recorded PM fixtures are QUARANTINED on purpose
    # (kept, not deleted — see scenarios.py `pm_selection`).
    for name in quarantined:
        assert name.startswith("run_"), f"unexpected quarantined fixture: {quarantined}"


def test_assert_admissible_raises_for_quarantined_fixture(tmp_path):
    manifest = _ok_manifest()
    manifest["filing"]["note"] = "quant_agent.db row 42"
    _write(tmp_path, "bad.json", manifest)
    with pytest.raises(fp.FixtureQuarantined):
        fp.assert_admissible("bad.json", directory=tmp_path)
