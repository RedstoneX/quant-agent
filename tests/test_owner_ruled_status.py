"""The `owner-ruled` ledger status must carry a date and a record."""

from src.number_sources import audit


def test_owner_ruled_status_without_a_date_or_record_fails(tmp_path) -> None:
    """The guard bites: an `owner-ruled` row must carry a date and a record."""
    import yaml as _yaml

    from src.number_sources import REPO_ROOT

    live = REPO_ROOT / "config" / "number_ledger.yaml"
    doc = _yaml.safe_load(live.read_text(encoding="utf-8"))
    rows = [r for r in doc["numbers"] if r.get("status") == "owner-ruled"]
    assert rows, "no owner-ruled row to mutate"
    assert audit(ledger_path=live) == []
    for missing, kind in (("ruled_on", "no-ruling-date"), ("ruling_record", "no-ruling-record")):
        saved = rows[0].pop(missing)
        broken = tmp_path / f"{missing}.yaml"
        broken.write_text(_yaml.safe_dump(doc), encoding="utf-8")
        assert kind in {p.kind for p in audit(ledger_path=broken)}
        rows[0][missing] = saved
    rows[0]["ruled_on"] = "last spring"
    bad = tmp_path / "bad.yaml"
    bad.write_text(_yaml.safe_dump(doc), encoding="utf-8")
    assert "no-ruling-date" in {p.kind for p in audit(ledger_path=bad)}
