"""Every status the desk can emit needs plain English in the rehearsal report.

PR 1115 moved the scan-failure status into src/intraday_scan_outcome.py
(`failed_scan_result`, whitelisted as a known call in test_rehearsal_report_verdict.py,
which is at its size cap); that derivation cannot read statuses built there,
check reads the status vocabularies directly instead of scanning return sites.
"""

from ops.rehearsal.report import STATUS_PLAIN
from src.intraday_scan_outcome import _BANNERS, failed_scan_result
from src.refusal_signature import NON_DECIDING_STATUSES

# Pre-existing gaps, found 2026-10-02. Shrink-only: fill one, delete it here.
_KNOWN_GAPS: set[str] = set()


def test_every_failed_scan_status_has_plain_english():
    class _Refused(RuntimeError):
        status_code = 402

    emitted = {
        failed_scan_result(_Refused("402"), "r")["status"],
        failed_scan_result(RuntimeError("boom"), "r")["status"],
        *_BANNERS,
    }
    assert emitted == {"intraday_scan_crashed", "intraday_scan_out_of_credit"}
    for status in emitted:
        assert STATUS_PLAIN[status].strip(), status


def test_out_of_credit_wording_matches_the_banner_meaning():
    plain = STATUS_PLAIN["intraday_scan_out_of_credit"]
    assert "no credit" in plain and "not a fault" in plain


def test_every_non_deciding_status_has_plain_english():
    missing = {s for s in NON_DECIDING_STATUSES if s not in STATUS_PLAIN}
    assert not (missing - _KNOWN_GAPS), sorted(missing - _KNOWN_GAPS)
    assert not (_KNOWN_GAPS - missing), "gap filled: remove from _KNOWN_GAPS"
