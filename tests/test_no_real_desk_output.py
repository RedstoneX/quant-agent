"""The build fails if a committed file carries REAL output from the live desk.

This repository is PUBLIC. Fixtures here were once built by copying real desk
output instead of inventing values — real tickers at real entry and stop
prices, real broker order ids, production log lines pasted verbatim. One of
those published stops was resting at the broker at the time.

`test_no_new_real_desk_output` is the guard. Everything else in this file
exists to keep it honest: the detector is proved against the real offenders,
against a fixture invented from scratch, and against the legitimate synthetic
fixtures it must never fire on.

If this test failed on YOUR change, read the failure message: it names the
file, the line and what to do. The answer is almost always "invent the number
instead", not "add the file to the allow-list".
"""

from __future__ import annotations

import pytest

from tests import desk_output_guard as guard

PROJECT_ROOT = guard.PROJECT_ROOT


@pytest.fixture(scope="module")
def audit() -> guard.Audit:
    return guard.audit_repo()


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

def test_no_new_real_desk_output(audit: guard.Audit) -> None:
    """No committed file carries real desk output unless it is allow-listed."""
    if not audit.new_files:
        return
    by_file: dict[str, list[guard.Finding]] = {}
    for f in audit.new_files:
        by_file.setdefault(f.path, []).append(f)
    report = [
        "",
        "This repository is PUBLIC. The content below looks like REAL output",
        "from the live trading desk — a real ticker at a real price, a real",
        "broker order id, or a production log line. Publishing it tells the",
        "world what the desk holds and where its stops are.",
        "",
    ]
    for path, findings in sorted(by_file.items()):
        report.append(f"--- {path}")
        for f in findings[:5]:
            report.append(f.render())
        if len(findings) > 5:
            report.append(f"    ... and {len(findings) - 5} more in this file")
        report.append("")
    report += [
        "WHAT TO DO: invent the values. A fixture does not need the real",
        "number to test the code — it needs a consistent one. Price things in",
        "whole dollars so a reader can see at a glance that nothing is live.",
        "",
        "Only if the file genuinely cannot work without real data, add it to",
        "ALLOWED in tests/desk_output_guard.py with a one-line reason and its",
        "current finding count. That is a reviewed decision, not a shortcut.",
    ]
    pytest.fail("\n".join(report), pytrace=False)


def test_allow_listed_files_have_not_grown_new_desk_output(audit: guard.Audit) -> None:
    """An allow-listed file may keep what it has; it may not accumulate more.

    Without this, the allow-list would be a licence: appending tomorrow's
    resting stop to an already-listed fixture would be invisible.
    """
    if not audit.over_ceiling:
        return
    lines = ["", "These files are allow-listed, but they now carry MORE real desk", "output than when they were allow-listed:", ""]
    for path, (ceiling, now) in sorted(audit.over_ceiling.items()):
        lines.append(f"  {path}: was {ceiling} finding(s), now {now}")
    lines.append("")
    for f in audit.over_ceiling_findings[:12]:
        lines.append(f.render())
    lines += [
        "",
        "WHAT TO DO: remove the newly added real values. Being on the",
        "allow-list excuses what was already published; it does not permit",
        "publishing more. If the growth is genuinely unavoidable, raise that",
        "file's count in ALLOWED and say why in the commit message.",
    ]
    pytest.fail("\n".join(lines), pytrace=False)


def test_every_allow_list_entry_still_earns_its_place(audit: guard.Audit) -> None:
    """An allow-list entry cannot outlive the problem it excuses.

    When a file is redacted or deleted, its entry must go too — otherwise the
    path stays permanently unguarded and the next author is free to paste real
    desk output straight back into it.
    """
    if not audit.stale_entries:
        return
    lines = ["", "Stale allow-list entries in tests/desk_output_guard.py:", ""]
    for path, why in sorted(audit.stale_entries.items()):
        lines.append(f"  {path}\n      {why}")
    pytest.fail("\n".join(lines), pytrace=False)


def test_oversize_files_are_accounted_for(audit: guard.Audit) -> None:
    """Only the first SCAN_BYTE_CAP characters of a file are scanned.

    That cap is a performance decision (one committed SEC corpus decompresses
    to 148 MB), and it would be a hiding place if it were silent. Any tracked
    file past the cap must be named in LARGE_BLOBS with a reason.
    """
    if not audit.oversize_unlisted:
        return
    pytest.fail(
        "\nThese tracked files are larger than the "
        f"{guard.SCAN_BYTE_CAP:,}-character scan cap, so only their opening "
        "section is checked for real desk output:\n\n"
        + "".join(f"  {p}\n" for p in audit.oversize_unlisted)
        + "\nWHAT TO DO: if the file is bulk third-party data (SEC filings, "
        "market bars), add it to LARGE_BLOBS in tests/desk_output_guard.py "
        "with a one-line reason. If it is desk output, it does not belong "
        "in a public repository at all.",
        pytrace=False,
    )


def test_allow_list_entries_all_carry_a_reason() -> None:
    for path, (reason, ceiling) in guard.allow_list().items():
        assert len(reason.split()) >= 6, f"{path}: reason is too thin to review"
        assert ceiling > 0, f"{path}: a ceiling of 0 means the entry is not needed"


# ---------------------------------------------------------------------------
# Proof the guard is load-bearing: it fires on the REAL offenders
# ---------------------------------------------------------------------------

#: One per signal, so a regression in any single detector shows up here.
KNOWN_OFFENDERS = [
    ("tests/fixtures/holding_why_rsg_20260917.json", "broker-order-id"),
    ("tests/fixtures/log_health_production_excerpt.txt", "production-log"),
    ("tests/fixtures/tech_answer_20260917_intra_check_26f52bf2_first.txt", "desk-decision"),
    ("tests/fixtures/tech_answer_20260917_intra_check_26f52bf2_retry.txt", "desk-decision"),
    ("tests/fixtures/constructor_drop_paths_archive.json", "desk-decision"),
    ("tests/fixtures/pm_response_11_targets_20260817.txt", "desk-prose"),
    ("tests/fixtures/pm_response_17_targets_20260820.txt", "desk-prose"),
    ("tests/test_stop_out_reconciliation.py", "broker-order-id"),
]


@pytest.mark.parametrize("relpath,signal", KNOWN_OFFENDERS)
def test_detector_fires_on_the_real_offenders(relpath: str, signal: str) -> None:
    """Each known-real file must still trip, by the signal it is known for.

    This is what stops the guard quietly rotting into a no-op. If someone
    loosens a threshold until nothing fires, these fail first.
    """
    text = guard.read_text(PROJECT_ROOT / relpath)
    assert text is not None, f"{relpath} is missing"
    signals = {f.signal for f in guard.scan_text(text[:guard.SCAN_BYTE_CAP], relpath)}
    assert signal in signals, (
        f"{relpath} no longer trips the {signal} detector (saw: {sorted(signals)}). "
        "Either the file was redacted — remove its allow-list entry — or the "
        "detector has been weakened."
    )


def test_detector_fires_on_a_freshly_invented_offender() -> None:
    """A brand-new fixture in the offending style, written for this test.

    The values below are invented, but they are shaped exactly like the real
    thing: a real ticker, cent-precision levels, the desk's own field names, a
    broker order id and a production log line. Every signal must fire.
    """
    new_fixture = '''{
  "symbol": "NVDA",
  "rating": "buy",
  "conviction": "high",
  "entry_price": 183.47,
  "stop_loss": 176.12,
  "reference_target": 201.63,
  "setup_type": "breakout",
  "thesis_invalid_if": "Price closes below MA50 ($176.12) on expanding volume",
  "broker_order_id": "3f81c0aa-91b4-4d02-ae15-7c9d2e6f08b3",
  "log": "2026-09-22 14:31:55,689 [ERROR] src.execution.broker: stop rejected for NVDA at 176.12",
  "pm_note": "Trimming NVDA and AAPL into strength while adding MSFT and AVGO on the pullback, and holding XOM as the energy hedge because the macro read still calls crude bid. The book is 62% invested and the cash drag is acceptable here, so no further deployment is warranted before the close."
}'''
    signals = {f.signal for f in guard.scan_text(new_fixture, "tests/fixtures/new.json")}
    assert signals == {"broker-order-id", "production-log", "desk-decision", "desk-prose"}, signals

    findings = guard.scan_text(new_fixture, "tests/fixtures/new.json")
    assert all(f.line > 0 for f in findings)
    assert all(f.remedy() for f in findings)


def test_the_guard_would_catch_this_fixture_if_it_were_committed() -> None:
    """The invented offender is caught with the allow-list applied too.

    An allow-list keyed on path must not accidentally excuse a new file just
    because it sits in a directory full of listed ones.
    """
    allowed = guard.allow_list()
    for neighbour in ("tests/fixtures/brand_new.json",
                      "ops/model_policy/results/brand-new.json"):
        assert neighbour not in allowed


# ---------------------------------------------------------------------------
# Proof the guard is quiet: it does NOT fire on legitimate synthetic fixtures
# ---------------------------------------------------------------------------

#: Real files in this repo that use real ticker strings and prices for good
#: reasons. A guard that fires on these gets switched off within a week.
LEGITIMATE_SYNTHETIC = [
    "tests/book_fixtures.py",                       # SGOV/SQQQ adversarial books
    "tests/test_analyst_verdict.py",                # a full SPY TechAnalysisResult
    "tests/test_api_evidence.py",                   # a full AAPL evidence record
    "tests/test_llm_output_null_tolerance.py",      # a deliberately broken SPY record
    "tests/test_notifier.py",                       # BA/MP orders with cent prices
    "tests/test_risk_based_sizing.py",              # dense cent-precision sizing maths
    "tests/test_phase3_exit_rework.py",             # OKLO exits at cent precision
    "tests/test_risk_verdict_per_symbol.py",        # XLE verdicts at cent precision
    "frontend/scripts/dashboard-visual-acceptance.mjs",  # a demo book for screenshots
    "src/api/static_cockpit/assets/index-Dx3KfNcH.js",   # a minified frontend bundle
]


@pytest.mark.parametrize("relpath", LEGITIMATE_SYNTHETIC)
def test_detector_is_silent_on_legitimate_synthetic_fixtures(relpath: str) -> None:
    path = PROJECT_ROOT / relpath
    if not path.exists():
        pytest.skip(f"{relpath} no longer exists")
    findings = guard.scan_text(guard.read_text(path) or "", relpath)
    assert not findings, (
        f"{relpath} is legitimate synthetic data but the guard fired:\n"
        + "\n".join(f.render() for f in findings[:3])
    )


def test_a_hand_written_fixture_in_the_recommended_style_is_silent() -> None:
    """The style the failure message tells authors to use must actually pass."""
    clean = '''{
  "symbol": "NVDA",
  "rating": "buy",
  "conviction": "high",
  "entry_price": 180.0,
  "stop_loss": 170.0,
  "reference_target": 200.0,
  "setup_type": "breakout",
  "thesis_invalid_if": "closes below 170",
  "broker_order_id": "00000000-0000-4000-8000-000000000000"
}'''
    assert guard.scan_text(clean, "tests/fixtures/clean.json") == []


def test_public_third_party_snapshots_are_not_mistaken_for_desk_output() -> None:
    """SEC and RSS snapshots carry UUIDs that belong to somebody else.

    EDGAR stamps every filing with `<!--r:...,g:...,d:...-->` and every RSS
    item carries a `<guid>`. Neither is a broker order id, and treating them
    as one is exactly the cry-wolf failure that gets a guard disabled.
    """
    edgar = "<!--r:019f1e90-6e28-7a7b-8903-606090e9262a,g:6624d3b3-1fad-451d-b3db-e3ee8e94be7d-->"
    rss = '<guid isPermaLink="false">e311a18a-b7e8-4bad-8c06-c8e60e484bcc</guid>'
    cdn = '<media:thumbnail url="https://ichef.bbci.co.uk/06bec250-b016-11f1-a540-61c3f7fc4e6c.jpg"/>'
    for sample in (edgar, rss, cdn):
        assert guard.scan_text(sample, "x.json") == [], sample


def test_documenting_the_log_format_is_not_a_paste() -> None:
    """`src/log_health.py` documents the log format in a comment. That is fine.

    A single format example whose message body carries no ticker, price or
    identifier is documentation. Two lines, or one line with live detail, is a
    paste.
    """
    documented = "#: `2026-09-18 13:47:27,710 [ERROR] src.pipeline: text`"
    assert guard.scan_text(documented, "src/log_health.py") == []

    pasted = (
        "2026-09-18 13:47:27,710 [ERROR] src.pipeline: stop for NVDA rejected\n"
        "2026-09-18 13:47:28,004 [ERROR] src.pipeline: retry scheduled\n"
    )
    assert {f.signal for f in guard.scan_text(pasted, "x.txt")} == {"production-log"}


# ---------------------------------------------------------------------------
# Hard to fool: the payload is checked, not the wrapper
# ---------------------------------------------------------------------------

def test_desk_output_hidden_inside_an_escaped_json_string_is_still_caught() -> None:
    """A model answer stored inside another JSON document is one escaped line.

    This is not a contrived evasion — it is how every file in
    `ops/model_policy/results` stores its answers.
    """
    wrapped = (
        '{"sample_output": "[\\n  {\\n    \\"symbol\\": \\"ORCL\\",\\n'
        '    \\"entry_price\\": 150.65,\\n    \\"stop_loss\\": 142.02,\\n'
        '    \\"reference_target\\": 159.52,\\n    \\"conviction\\": \\"medium\\",\\n'
        '    \\"setup_type\\": \\"range\\"\\n  }\\n]"}'
    )
    assert "desk-decision" in {f.signal for f in guard.scan_text(wrapped, "x.json")}


def test_desk_output_in_single_line_json_is_still_caught() -> None:
    """Whether a fixture is pretty-printed is a formatting choice.

    `holding_why_rsg_20260917.json` is one 64,000-character line; a guard that
    reads lines would see nothing in it.
    """
    import json as _json

    record = {
        "symbol": "ORCL", "entry_price": 150.65, "stop_loss": 142.02,
        "reference_target": 159.52, "conviction": "medium", "setup_type": "range",
    }
    one_line = _json.dumps(record, separators=(",", ":"))
    assert "\n" not in one_line
    assert "desk-decision" in {f.signal for f in guard.scan_text(one_line, "x.json")}


def test_desk_output_in_a_gzipped_fixture_is_still_caught(tmp_path) -> None:
    """`ops/model_policy/fixtures` commits nine gzipped blobs."""
    import gzip as _gzip

    payload = (
        '{"broker_order_id": "3f81c0aa-91b4-4d02-ae15-7c9d2e6f08b3", '
        '"status": "filled", "symbol": "RSG"}'
    )
    blob = tmp_path / "snapshot.json.gz"
    blob.write_bytes(_gzip.compress(payload.encode()))
    text = guard.read_text(blob)
    assert text is not None
    assert "broker-order-id" in {f.signal for f in guard.scan_text(text, "snapshot.json.gz")}


def test_the_guard_does_not_key_off_filenames() -> None:
    """Renaming a fixture must not change the verdict."""
    payload = '{"broker_order_id": "3f81c0aa-91b4-4d02-ae15-7c9d2e6f08b3", "status": "filled"}'
    names = ["tests/fixtures/x.json", "README.md", "src/prod.py", "docs/notes.txt", "a.csv"]
    verdicts = [{f.signal for f in guard.scan_text(payload, n)} for n in names]
    assert all(v == verdicts[0] for v in verdicts)


def test_placeholder_identifiers_are_not_flagged() -> None:
    for fake in (guard.PLACEHOLDER_UUID,
                 "11111111-1111-1111-1111-111111111111",
                 "deadbeef-0000-4000-8000-000000000000"):
        payload = f'{{"broker_order_id": "{fake}", "status": "filled"}}'
        assert guard.scan_text(payload, "x.json") == [], fake


def test_the_specimen_exclusion_is_exactly_this_one_file() -> None:
    """SPECIMEN_FILES must never grow into a general hiding place.

    This test's own trip-strings above are the only reason a tracked file is
    ever excluded from the scan. If a second path is ever added here, it needs
    the same scrutiny this one got — not a rubber stamp.
    """
    assert guard.SPECIMEN_FILES == frozenset({"tests/test_no_real_desk_output.py"})
