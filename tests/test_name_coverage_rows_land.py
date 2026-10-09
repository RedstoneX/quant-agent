"""The evidence gate must leave real per-name rows in the store."""

from tests.test_evidence_gate import _pipeline, _run


def test_name_coverage_rows_actually_land_in_the_store():
    """The per-name record must PRODUCE rows, not merely be invoked.

    The call used to pass `symbol` twice (by name and inside the unpacked
    details), raising a TypeError that a broad catch turned into a log
    line, so the store never held one per-name coverage row.
    """
    import json

    p = _pipeline({"macro": "ok", "tech": "ok"})
    _run(p)
    rows = [
        c.kwargs for c in p.db.insert_specialist_evidence.call_args_list if c.kwargs.get("kind") == "pipeline_event"
    ]
    coverage = [
        r for r in rows if r.get("symbol") == "NVDA" and json.loads(r["evidence_json"]).get("gate") == "name_coverage"
    ]
    assert coverage, "no per-name name_coverage row reached the store for NVDA"
    payload = json.loads(coverage[-1]["evidence_json"])
    assert payload["stage"] == "evidence_gate"
    assert payload["outcome"] == "recorded"
