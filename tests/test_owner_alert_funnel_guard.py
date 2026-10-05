"""The owner-alert funnel is the only send path, and a failed send is visible.

Two things are pinned here. First, ``scripts/owner_alert_funnel_guard.py``
refuses a NEW place that builds its own messenger instead of going through
``TelegramNotifier.send`` -- the one path that writes the ``notifier_sends``
row the dashboard surfaces. Second, the funnel really does write that row when
a send fails, so a dropped owner alert does not look like a delivered one.

The guard is absolute, not a delta against the trunk: the list of bypasses is
short and reasoned, so it lives in code and needs no reference commit. Both
exemptions are proved load-bearing below -- remove either and its site is
reported -- which is what keeps them narrow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts import owner_alert_funnel_guard as g
from src.notifier import owner_alert_delivery as delivery
from src.notifier import send_owner_alert
from src.notifier.transport import TelegramNotifier

NEW_URL_FILE = "src/some_new_alerting_module.py"


def test_tree_has_no_send_path_outside_the_funnel():
    assert not g.violations(), (
        "Owner-alert send path(s) outside the funnel:\n"
        + "\n".join(f"  - {b}" for b in g.violations()) + "\n" + g.FIX_ADVICE
    )


def test_guard_refuses_a_new_file_that_builds_the_bot_api_url(monkeypatch):
    monkeypatch.setattr(g, "scanned_paths", lambda: [NEW_URL_FILE, g.FUNNEL_FILE])
    monkeypatch.setattr(
        g, "read",
        lambda p: f"requests.post('https://{g.TELEGRAM_URL_MARKER}X/sendMessage')",
    )
    monkeypatch.setattr(g, "sender_classes", lambda paths: set())
    bad = g.violations()
    assert any(NEW_URL_FILE in b for b in bad), bad


def test_guard_does_not_scan_itself_but_still_scans_lookalikes(monkeypatch):
    class Done:
        returncode = 0
        stderr = ""
        stdout = "\0".join(
            [g.GUARD_FILE, "scripts/owner_alert_funnel_guard2.py",
             "src/scripts/owner_alert_funnel_guard.py", g.FUNNEL_FILE]
        )

    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: Done())
    got = g.scanned_paths()
    assert g.GUARD_FILE not in got
    assert "scripts/owner_alert_funnel_guard2.py" in got
    assert "src/scripts/owner_alert_funnel_guard.py" in got


def test_guard_still_bites_a_bypass_in_a_file_named_like_the_guard(monkeypatch):
    lookalike = "scripts/owner_alert_funnel_guard_copy.py"
    monkeypatch.setattr(g, "scanned_paths", lambda: [lookalike, g.FUNNEL_FILE])
    monkeypatch.setattr(
        g, "read",
        lambda p: f"requests.post('https://{g.TELEGRAM_URL_MARKER}X/sendMessage')",
    )
    monkeypatch.setattr(g, "sender_classes", lambda paths: set())
    assert any(lookalike in b for b in g.violations())


def test_guard_refuses_a_new_stand_in_notifier_class(monkeypatch):
    monkeypatch.setattr(g, "scanned_paths", lambda: [])
    monkeypatch.setattr(g, "url_sites", lambda paths: set(g.EXEMPT_URL_SITES))
    monkeypatch.setattr(
        g, "sender_classes",
        lambda paths: set(g.EXEMPT_SENDER_CLASSES) | {("src/newthing.py", "QuietNotifier")},
    )
    bad = g.violations()
    assert any("QuietNotifier" in b for b in bad), bad


@pytest.mark.parametrize("site", sorted(g.EXEMPT_SENDER_CLASSES))
def test_each_class_exemption_is_load_bearing_and_narrow(site, monkeypatch):
    """Drop one exemption and exactly its own site is reported -- nothing else.

    An exemption that changes nothing when removed is covering a site that no
    longer exists, and a blanket one would silence more than itself.
    """
    trimmed = {k: v for k, v in g.EXEMPT_SENDER_CLASSES.items() if k != site}
    monkeypatch.setattr(g, "EXEMPT_SENDER_CLASSES", trimmed)
    bad = g.violations()
    assert [b for b in bad if site[1] in b], f"{site} exemption covers nothing"
    assert len(bad) == 1, f"removing one exemption changed more than its site: {bad}"


def test_dropping_the_shell_wrapper_exemption_reports_it(monkeypatch):
    trimmed = {k: v for k, v in g.EXEMPT_URL_SITES.items() if k == g.FUNNEL_FILE}
    monkeypatch.setattr(g, "EXEMPT_URL_SITES", trimmed)
    bad = g.violations()
    assert any("run_if_et_window.sh" in b for b in bad), bad


def test_a_dead_exemption_is_itself_a_failure(monkeypatch):
    monkeypatch.setattr(
        g, "EXEMPT_SENDER_CLASSES",
        {**g.EXEMPT_SENDER_CLASSES, ("src/gone.py", "Removed"): "stale"},
    )
    assert any("no longer exists" in b for b in g.violations())


def test_guard_refuses_loudly_when_it_cannot_list_the_tree(monkeypatch, capsys):
    def _blow_up():
        raise g.TreeUnreadable("git ls-files failed")

    monkeypatch.setattr(g, "scanned_paths", _blow_up)
    assert g.main([]) == 2
    assert "REFUSING" in capsys.readouterr().err


def test_guard_refuses_rather_than_passes_on_an_unparseable_file(tmp_path, monkeypatch):
    bad_file = "src/broken_syntax.py"
    monkeypatch.setattr(g, "read", lambda p: "class X:\n  def send(self) ->\n")
    with pytest.raises(g.TreeUnreadable):
        g.sender_classes([bad_file])


def test_a_failed_owner_alert_leaves_an_undelivered_row(tmp_path, monkeypatch):
    """The funnel's whole point: a send that fails is visibly distinct.

    Without the row, a page about a naked position that never reached the owner
    reads exactly like one that did -- and he reads the dashboard and nothing
    else. No network is touched; requests.post is replaced outright.
    """
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)

    monkeypatch.setattr(delivery, "RETRY_DELAYS_S", (0.0,) * delivery.MAX_ATTEMPTS)
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.side_effect = requests.ConnectionError("egress blocked")
        assert send_owner_alert("EXIT NOT PLACED\nAAPL has no stop") is False

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT kind, status, detail FROM notifier_sends ORDER BY id"
        )]
    statuses = [r["status"] for r in rows]
    assert statuses.count("failed") == delivery.MAX_ATTEMPTS, rows
    assert statuses[-1] == delivery.UNDELIVERED_STATUS, rows
    assert rows[-1]["kind"] == "owner_alert"
    assert "undelivered_total=1" in rows[-1]["detail"]
    assert "egress blocked" in rows[0]["detail"]


def test_a_delivered_owner_alert_is_not_recorded_as_undelivered(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "quant_agent.db"
    monkeypatch.setattr("src.notifier._DB_PATH", db_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)

    with patch("src.notifier.requests.post") as mock_post:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        assert send_owner_alert("EXIT NOT PLACED\nAAPL has no stop") is True

    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        statuses = [r[0] for r in conn.execute("SELECT status FROM notifier_sends")]
    assert statuses == ["sent"]


def test_guard_refuses_a_new_direct_notifier_send(monkeypatch):
    """Rule 3 is a trunk delta: a send this branch added is refused."""
    monkeypatch.setattr(g, "scanned_paths", lambda: [])
    monkeypatch.setattr(g, "url_sites", lambda paths: set(g.EXEMPT_URL_SITES))
    monkeypatch.setattr(g, "sender_classes", lambda paths: set(g.EXEMPT_SENDER_CLASSES))
    monkeypatch.setattr(
        g, "direct_send_sites",
        lambda paths: {"src/newthing.py": ["notifier.send(msg)"]},
    )
    monkeypatch.setattr(g, "trunk_direct_send_sites", lambda paths: {})
    assert any("newthing.py" in b for b in g.violations())


def test_rule_three_refuses_when_the_trunk_cannot_be_read(monkeypatch):
    monkeypatch.setattr(g, "url_sites", lambda paths: set(g.EXEMPT_URL_SITES))
    monkeypatch.setattr(g, "sender_classes", lambda paths: set(g.EXEMPT_SENDER_CLASSES))
    monkeypatch.setattr(g, "TRUNK", "origin/no-such-ref-for-this-test")
    with pytest.raises(g.TreeUnreadable):
        g.violations()


def test_an_unchanged_existing_direct_send_is_not_a_violation(monkeypatch):
    """Removals and pre-existing sites never fail; only an added one does."""
    same = {"src/trader_feed/naked.py": ["notifier.send(x)"]}
    monkeypatch.setattr(g, "scanned_paths", lambda: [])
    monkeypatch.setattr(g, "url_sites", lambda paths: set(g.EXEMPT_URL_SITES))
    monkeypatch.setattr(g, "sender_classes", lambda paths: set(g.EXEMPT_SENDER_CLASSES))
    monkeypatch.setattr(g, "direct_send_sites", lambda paths: same)
    monkeypatch.setattr(g, "trunk_direct_send_sites", lambda paths: same)
    assert not g.violations()
