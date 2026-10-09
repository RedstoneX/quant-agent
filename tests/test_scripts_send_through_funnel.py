"""Every owner-facing send under scripts/ goes through the retry funnel. ABSOLUTE.

The funnel guard's direct-send rule is a delta against the trunk, so the nine
bare ``notifier.send(...)`` calls the ops scripts carried were grandfathered
for weeks (docs/SPLIT_DEFERRED_FINDINGS.md). A bare send has no retry and
writes no counted ``owner_alert_undelivered`` row, so a lost ops alert read
as a delivered one. This rule stores no baseline and compares to no trunk:
under ``scripts/`` the only direct ``.send(`` allowed is the manual probe,
named by identity. A new bare send fails here even when the trunk read that
rule 3 depends on is stale or cached.
"""

from __future__ import annotations

from scripts import owner_alert_funnel_guard as g

#: By path, with the reason. Never a count.
EXEMPT = {
    "scripts/telegram_test.py": "manual one-shot probe run by a human at a keyboard",
}


def _script_direct_sends() -> dict[str, list[str]]:
    paths = [p for p in g.scanned_paths() if p.startswith("scripts/")]
    return g.direct_send_sites(paths)


def test_no_script_sends_around_the_funnel():
    offenders = {p: ls for p, ls in _script_direct_sends().items() if p not in EXEMPT}
    assert offenders == {}, (
        "bare notifier send under scripts/: route it through "
        "src.notifier.owner_alert_delivery.deliver_with_retry (or send_owner_alert) "
        f"so a failure is retried and counted: {offenders}"
    )


def test_exemption_is_by_identity_and_alive():
    found = _script_direct_sends()
    assert set(EXEMPT) <= set(found), "a dead exemption widens the hole; delete it"
    assert all(p.startswith("scripts/") for p in EXEMPT)


def test_a_planted_bare_send_is_caught(monkeypatch, tmp_path):
    planted = "scripts/_planted_probe.py"
    monkeypatch.setattr(g, "scanned_paths", lambda: [planted])
    monkeypatch.setattr(g, "read", lambda path: "notifier.send(message)\n")
    assert planted in _script_direct_sends()
