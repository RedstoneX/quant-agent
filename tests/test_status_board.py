"""The status board must never report a wrong status confidently.

The board exists because every hand-maintained status document in this repo
went stale — five wrong claims inside two days. Its value therefore rests
entirely on one property: **it would rather say `unknown` than say something
false.** These tests pin that property, and they pin the specific bug the very
first live run exposed, where a malformed rule masqueraded as documentation rot.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.api.server import _freshness_banner

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "status_board.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_board", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules; register before exec
    sys.modules["status_board"] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load()


# --------------------------------------------------------------------------
# a bent ruler is not a broken system
# --------------------------------------------------------------------------

def test_prose_where_a_test_name_belongs_is_unknown_not_failure():
    """The bug the first real run found.

    `docs/phases.yaml` carried a `test_exists` rule whose `test` field held a
    sentence rather than a test identifier. The file it pointed at was
    perfectly fine, but the sentence was obviously not inside it, so the rule
    failed and the board announced Phase 1 as CONTRADICTED — documentation rot
    that did not exist.

    A rule the board cannot evaluate is a broken instrument. It reports
    `unknown` and says why. It never reports rot.
    """
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": "test_status_board.py exists as a dedicated module (27 tests per the spec)",
    }
    result = sb.check_rule(rule, {})
    assert result.verdict == sb.UNKNOWN
    assert "malformed" in result.detail


def test_a_real_test_name_that_is_present_passes():
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": "test_a_real_test_name_that_is_present_passes",
    }
    assert sb.check_rule(rule, {}).verdict == sb.PASS


def test_a_real_test_name_that_is_absent_fails():
    """A genuine absence must still fail — the malformed-rule guard must not
    become a blanket excuse that swallows real regressions.

    The absent name is assembled at runtime on purpose. Written as a literal it
    would appear in this very file, and the substring search would find it and
    pass — which is how this test failed the first time it ran.
    """
    absent = "test_" + "absent" + "_sentinel_" + "name"
    rule = {
        "kind": "test_exists",
        "path": "tests/test_status_board.py",
        "test": absent,
    }
    assert sb.check_rule(rule, {}).verdict == sb.FAIL


def test_an_unrecognised_rule_kind_is_unknown():
    assert sb.check_rule({"kind": "haruspicy"}, {}).verdict == sb.UNKNOWN


def test_a_manual_rule_is_unknown_never_pass():
    """`manual` means a human must look. It must never silently count as proof."""
    r = sb.check_rule({"kind": "manual", "note": "someone check the box"}, {})
    assert r.verdict == sb.UNKNOWN


# --------------------------------------------------------------------------
# settings rules
# --------------------------------------------------------------------------

def test_setting_equals_reads_nested_keys():
    cfg = {"risk": {"max_position_pct": 20}}
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, cfg
    )
    assert r.verdict == sb.PASS


def test_setting_equals_fails_on_a_changed_value():
    cfg = {"risk": {"max_position_pct": 35}}
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, cfg
    )
    assert r.verdict == sb.FAIL
    assert "35" in r.detail


def test_setting_equals_fails_when_the_key_is_gone():
    r = sb.check_rule(
        {"kind": "setting_equals", "key": "risk.max_position_pct", "value": 20}, {}
    )
    assert r.verdict == sb.FAIL


def test_setting_present_passes_when_the_key_exists_regardless_of_value():
    """`setting_present` makes no claim about the value, only that the key is
    there at all — the shape needed for a stopgap setting that is expected to
    be re-tuned over time without that re-tuning reading as rot."""
    cfg = {"llm_cost_circuit": {"max_free_failure_sessions_per_mode": 40}}
    r = sb.check_rule(
        {"kind": "setting_present",
         "key": "llm_cost_circuit.max_free_failure_sessions_per_mode"}, cfg,
    )
    assert r.verdict == sb.PASS


def test_setting_present_fails_when_the_key_is_gone():
    r = sb.check_rule(
        {"kind": "setting_present",
         "key": "llm_cost_circuit.max_free_failure_sessions_per_mode"}, {},
    )
    assert r.verdict == sb.FAIL


def test_missing_file_fails_rather_than_erroring():
    r = sb.check_rule({"kind": "file_exists", "path": "src/definitely_not_here.py"}, {})
    assert r.verdict == sb.FAIL


# --------------------------------------------------------------------------
# the verdict, which is the whole point
# --------------------------------------------------------------------------

def _phase(results):
    p = sb.PhaseView(id="x", title="t", summary="s", recorded="DONE AND LIVE",
                     confidence="high")
    p.results = results
    return p


def test_one_failing_rule_contradicts_the_whole_phase():
    """A phase is only as good as its weakest proof. One broken check is
    enough — the board must not average rot away."""
    p = _phase([
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("file_exists", sb.FAIL, ""),
    ])
    assert p.verdict == "CONTRADICTED"


def test_a_phase_with_nothing_checkable_is_unverified_not_confirmed():
    """The dangerous case: a phase whose evidence is entirely `manual`. It must
    never read as confirmed just because nothing disproved it."""
    p = _phase([
        sb.RuleResult("manual", sb.UNKNOWN, ""),
        sb.RuleResult("manual", sb.UNKNOWN, ""),
    ])
    assert p.verdict == "UNVERIFIED"


def test_unknowns_alongside_passes_do_not_block_confirmation():
    p = _phase([
        sb.RuleResult("file_exists", sb.PASS, ""),
        sb.RuleResult("manual", sb.UNKNOWN, ""),
    ])
    assert p.verdict == "CONFIRMED"
    assert p.unknown == 1


# --------------------------------------------------------------------------
# the shipped manifest must actually be evaluable
# --------------------------------------------------------------------------

def test_the_real_manifest_parses_and_every_rule_is_well_formed():
    """Guards the manifest itself. Every rule must be one the board understands
    and can evaluate — a rule it can only ever answer `unknown` to is dead
    weight dressed as evidence, and the one exception is `manual`, which is
    honest about needing a person."""
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    assert phases, "manifest carries no phases"

    known = {"commit_in_main", "pr_merged", "file_exists", "symbol_in_file",
             "test_exists", "setting_equals", "setting_present", "manual"}
    malformed = []
    for entry in phases:
        assert entry.get("id"), "every phase needs an id"
        assert entry.get("plain_summary"), f"{entry.get('id')} has no plain-English summary"
        for rule in entry.get("evidence") or []:
            kind = rule.get("kind")
            assert kind in known, f"{entry['id']}: unknown rule kind {kind!r}"
            if kind == "test_exists" and not sb._IDENTIFIER.match(str(rule.get("test", ""))):
                malformed.append((entry["id"], rule.get("test")))
            if kind == "symbol_in_file":
                assert rule.get("path"), f"{entry['id']}: symbol rule with no path"
    assert not malformed, f"test_exists rules carrying prose instead of a test name: {malformed}"


def _assert_mechanical_rule_unless_open(entry: dict) -> None:
    """The shared check behind the mechanical-rule invariant.

    A phase whose `status` is `OPEN` is exempt: that status marks a defects
    log, not a completion claim — the whole point of `open_defects` is
    "these bugs still exist," and a mechanical rule can only assert a bug's
    continued existence by re-detecting it, which means the rule flips to
    failing (and the board's CONTRADICTED alarm fires) at the exact moment
    someone fixes the bug — punishing the fix, not the documentation. Every
    other status describes work in progress or work claimed done —
    `NOT STARTED`, `PARTIAL`, `DONE AND LIVE` — and must still carry at
    least one machine-checkable rule; there is no excuse for those to rest
    on `manual` alone.

    Both `test_every_phase_has_at_least_one_mechanical_rule` (the real
    manifest) and `test_the_open_exemption_does_not_silently_widen` (fake
    entries pinning the boundary) call this one function, so the two checks
    cannot drift apart.
    """
    if entry.get("status") == "OPEN":
        return
    rules = entry.get("evidence") or []
    mechanical = [r for r in rules if r.get("kind") != "manual"]
    assert mechanical, (
        f"{entry.get('id')} has only manual evidence — it can never be verified"
    )


def test_every_phase_has_at_least_one_mechanical_rule():
    """A phase resting entirely on `manual` cannot be verified by the board, so
    it must be visible as such rather than quietly trusted.

    One exemption: a phase whose `status` is `OPEN`. That status marks a
    defects log, not a completion claim — the whole point of `open_defects`
    is "these bugs still exist," and a mechanical rule can only assert a
    bug's continued existence by re-detecting it, which means the rule flips
    to failing (and the board's CONTRADICTED alarm fires) at the exact
    moment someone fixes the bug — punishing the fix, not the documentation.
    This invariant exists to stop a phase claiming DONE on no proof; an OPEN
    log makes no such claim, so it is exempt. NOT STARTED and PARTIAL carry
    no such excuse and stay covered — both have real mechanical rules today.
    """
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    for entry in phases:
        _assert_mechanical_rule_unless_open(entry)


def test_the_open_exemption_does_not_silently_widen():
    """Pins `_assert_mechanical_rule_unless_open` to exactly the `OPEN`
    status, using fake in-memory entries rather than the real manifest, so
    the boundary stays covered even if docs/phases.yaml changes shape or
    every entry there happens to carry a mechanical rule already.

    Without this test, someone could widen the exemption in
    `_assert_mechanical_rule_unless_open` (say, to also cover `PARTIAL` or
    `NOT STARTED`) and nothing would fail until a real phase quietly shipped
    on manual-only evidence.
    """
    manual_only = [{"kind": "manual", "note": "someone check the box"}]

    open_entry = {"id": "fake_open", "status": "OPEN", "evidence": manual_only}
    partial_entry = {"id": "fake_partial", "status": "PARTIAL", "evidence": manual_only}
    not_started_entry = {
        "id": "fake_not_started", "status": "NOT STARTED", "evidence": manual_only,
    }

    _assert_mechanical_rule_unless_open(open_entry)  # must not raise

    with pytest.raises(AssertionError):
        _assert_mechanical_rule_unless_open(partial_entry)

    with pytest.raises(AssertionError):
        _assert_mechanical_rule_unless_open(not_started_entry)


def test_the_template_carries_every_placeholder_the_renderer_fills():
    """A renamed placeholder would silently ship a page with `{{SPEND}}` printed
    on it. Cheap to catch here."""
    template = (Path(__file__).resolve().parents[1]
                / "scripts" / "status_board_template.html").read_text()
    for key in ("{{STAMP}}", "{{BUILT_SHA}}", "{{DEPLOY}}", "{{CIRCUIT}}", "{{SPEND}}",
                "{{SPEND_PCT}}", "{{SPEND_NOTE}}", "{{SESSIONS}}", "{{ROWS}}", "{{ALARM}}",
                "{{RULES_TOTAL}}", "{{RULES_PASS}}", "{{RULES_FAIL}}",
                "{{RULES_UNKNOWN}}", "{{BOX_SHA}}", "{{MAIN_SHA}}", "{{JARGON_BANNER}}"):
        assert key in template, f"template is missing {key}"


def test_rendered_output_leaves_no_placeholder_behind(tmp_path):
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.66,
             "sessions_today": 14, "box_sha": "abc123", "main_sha": "abc123"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template)
    assert "{{" not in out, "an unfilled placeholder reached the rendered page"


def test_unknown_live_values_render_as_unknown_not_as_zero():
    """If the box cannot be read, the page must say so. A silent 0 would be a
    lie of exactly the kind this board exists to stop."""
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": None, "circuit": None, "spend_today": None,
             "sessions_today": None, "box_sha": None, "main_sha": None}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template)
    assert "unknown" in out
    assert "$0.00" not in out


def _git(repo: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "board-test", "GIT_AUTHOR_EMAIL": "board-test@example.com",
        "GIT_COMMITTER_NAME": "board-test", "GIT_COMMITTER_EMAIL": "board-test@example.com",
    }
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                    text=True, env=env)


def _repo_with_merged_pr(tmp_path: Path, number: int) -> Path:
    """A throwaway repo with a known merge commit, standing in for main.

    The real test target — `git log origin/main --merges --grep=...` — must
    not depend on the ambient checkout's history. On a developer's machine
    that history is full; in GitHub Actions the PR job checks out a shallow
    (fetch-depth: 1) clone of the ephemeral `pull/N/merge` ref, which carries
    no `origin/main` ref and no historical merge commits at all. A test that
    only passes on a full clone is not pinning the behaviour, it's pinning the
    environment it happened to be written in. Building the fixture here makes
    the result independent of clone depth or which repo happens to be checked
    out.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("feature\n")
    _git(repo, "commit", "-q", "-am", "feature work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff",
         "-m", f"Merge pull request #{number} from RedstoneX/feature", "feature")
    # `check_rule` looks up `origin/main` by name; give this throwaway repo a
    # ref with that name rather than an actual remote, which it doesn't need.
    _git(repo, "branch", "origin/main", "main")
    return repo


def test_merged_pr_rules_resolve_from_git_without_a_github_credential(tmp_path):
    """The board runs on the production box, where `gh` exists but the runtime
    account is deliberately NOT authenticated — putting a GitHub token on the
    account that trades is the owner's decision, not this script's convenience.

    A merged PR leaves its own merge commit in main, which is the same fact
    with no credential attached. Without this path, 13 of the manifest's rules
    would report `unknown` on the box for no good reason.
    """
    repo = _repo_with_merged_pr(tmp_path, 102)
    r = sb.check_rule({"kind": "pr_merged", "number": 102}, {}, repo_root=repo)
    assert r.verdict == sb.PASS
    assert "merge commit" in r.detail


def test_an_unmerged_pr_is_not_reported_as_merged_from_git_alone():
    """Absence of a merge commit is not proof of absence — a squash merge
    leaves none — so the git path must never turn a miss into a FAIL on its
    own. It falls through to GitHub, and to `unknown` if that is unreachable.
    """
    r = sb.check_rule({"kind": "pr_merged", "number": 999999}, {})
    assert r.verdict in (sb.FAIL, sb.UNKNOWN)
    assert r.verdict != sb.PASS


# --------------------------------------------------------------------------
# documentation-hygiene guards
#
# `docs/WORK.md` and `docs/phases.yaml` are the two documents a session reads
# before doing anything else. Both went stale the same way every other
# status document in this repo went stale: not through one dramatic error,
# but through years of small, individually-reasonable additions that nobody
# ever removed. A 2026-08-31 pass cut the append-only "Correction 20XX:"
# notes, split a 6,405-character wall of prose into a ranked, enumerable
# defects list, and cut WORK.md down to only its genuinely open sections.
# These four checks exist so that pass has to be re-done by hand, one commit
# at a time, if it is ever eroded again — the same rot this file's other
# tests already guard against, applied to the documents instead of the code.
# --------------------------------------------------------------------------

def test_phases_manifest_carries_no_correction_clauses():
    """`docs/phases.yaml` used to correct itself in place: a later editor
    would leave the wrong sentence standing and prepend "Correction 20XX:
    this previously said X; that is now false; Y" rather than just writing
    Y. Eighteen of these had accumulated in one file by 2026-08-30, each one
    forcing a reader to hold a claim, its correction, and sometimes a second
    correction of the correction, all at once just to find out what is true
    today. Git history is the append-only record; the manifest itself only
    needs to say what is true now. If this ever fires, the fix is to fold
    the correction into the sentence it corrects and delete the note — not
    to add a nineteenth one.
    """
    import re

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    text = manifest.read_text()
    hits = re.findall(r"Correction 20\d\d", text)
    assert not hits, (
        f"{len(hits)} correction clause(s) still in docs/phases.yaml — fold "
        "each into the sentence it corrects instead of layering a new note "
        "on top"
    )


def test_no_plain_summary_exceeds_two_thousand_characters():
    """Every `plain_summary` renders verbatim onto the owner's status board —
    he reads the board, not this file. One entry had grown to 6,405
    characters, a single paragraph burying roughly a dozen distinct defects
    that a reader had to excavate by hand — that is the shape this guard
    exists to catch. A 1,000-character cap was tried first and immediately
    failed five legitimate entries (phase_4/5/6/7/9), each one a single dense
    paragraph describing one already-finished phase rather than several
    distinct items — the longest, phase_6, measured 1,883 characters with
    nothing left to split out. 2,000 characters clears that observed
    distribution with modest headroom (~6% over the current max) while
    remaining well under a third of the 6,405-character violation that
    prompted this guard, so a return to that kind of undifferentiated bloat
    still trips it. If this fires, the fix is still the same: break the
    entry into its own list (as `open_defects` now does, in its `defects:`
    field) rather than raising the number again.
    """
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    too_long = [
        (e.get("id"), len(e.get("plain_summary", "")))
        for e in phases
        if len(e.get("plain_summary", "")) > 2000
    ]
    assert not too_long, f"plain_summary over 2000 characters: {too_long}"


def test_open_defects_is_a_ranked_list_not_a_paragraph():
    """`open_defects` used to be a single plain_summary paragraph with the
    individual defects buried inside it as parenthetical letters — (a), (b),
    (c) — readable only by reading the whole paragraph start to finish. A
    session that needed to know the single highest-priority open defect had
    no way to get that answer without reading all of them. `defects:` is a
    real list precisely so a session (or a script) can read `rank: 1` and
    stop, instead of parsing prose to reconstruct an order that was never
    machine-readable in the first place.

    An empty list is a legitimate value, not a regression back to prose: the
    repo's own convention (see PR #180, and the 2026-08-31 closure that
    emptied the list entirely) is to remove a defect from this list the same
    commit that closes it, recording the closure as evidence instead. Zero
    open defects is the list doing its job, not losing its shape — so this
    only pins that `defects:` stays a real list, and that whatever entries
    it does carry are well-formed.
    """
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    hit = [e for e in phases if str(e.get("id")) == "open_defects"]
    assert hit, "no open_defects entry in the manifest"
    defects = hit[0].get("defects")
    assert isinstance(defects, list), "open_defects.defects must be a list"
    for d in defects:
        for field in ("id", "title", "rank", "status"):
            assert d.get(field) not in (None, ""), (
                f"defect {d.get('id', d)!r} is missing required field {field!r}"
            )


def test_work_md_stays_under_a_hundred_thousand_bytes():
    """`docs/WORK.md` used to be 132,932 bytes — a session had to read the
    whole thing to find the two or three items it actually needed, because
    finished work, ratified decisions and genuinely open items had all been
    appended to the same file for months and nothing was ever removed. The
    2026-08-31 pass cut it down to only its open (and a few still-unsure)
    sections; finished work was deleted (git history keeps it) and standing
    rules/decisions moved into `AGENTS.md`, which does not carry this cap
    because it is a curated contract, not an append-only log.

    A 20,000-byte cap was tried first and was never reachable: even after
    that cut, the file measured 94,801 bytes, almost all of it the "Ordered
    backlog" section — real, unfinished work, not clutter, and out of scope
    to prune on a documentation-hygiene pass. 100,000 bytes gives that
    measured size about 5,200 bytes (~5%) of headroom for small edits
    without being a target to fill. It is still a tripwire, not a budget: if
    WORK.md ever creeps past it, that means finished or decided material has
    crept back in and needs the same cut-and-move treatment again — check
    for stale CLOSED/DECISION sections before raising this number.
    """
    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    if not work_md.exists():
        return
    size = work_md.stat().st_size
    assert size <= 100_000, (
        f"docs/WORK.md is {size} bytes, over the 100,000-byte cap — finished "
        "or decided content has likely crept back in; MOVE it to "
        "docs/INCIDENT_HISTORY.md rather than deleting it, and never raise this "
        "number to make room"
    )


def test_finished_work_has_somewhere_to_go_that_is_not_deletion():
    """The cap above used to be satisfied by DELETING finished work, on the
    grounds that git history keeps it. The owner is not a developer and does
    not read git, so in practice that erased the record of what had gone
    wrong at exactly the point it became history — and it erased it on a
    schedule, every time the backlog filled up.

    `docs/INCIDENT_HISTORY.md` is the destination: append-only, never trimmed, one
    plain-language line per entry stating what actually broke. This test is
    the mechanical half of that rule. A prose instruction telling future
    sessions to prune into the log is exactly the kind of thing that gets
    followed twice and then forgotten; the pointer being load-bearing on a
    passing test is not.

    Asserted here rather than trusted: the log exists, WORK.md tells a
    session where to put finished work, and the log has not been quietly
    emptied to keep some other budget happy.
    """
    root = Path(__file__).resolve().parents[1]
    work_md = root / "docs" / "WORK.md"
    defect_log = root / "docs" / "INCIDENT_HISTORY.md"
    if not work_md.exists():
        return

    assert defect_log.exists(), (
        "docs/INCIDENT_HISTORY.md is missing. WORK.md is capped and its finished "
        "content has to go somewhere other than /dev/null — recreate the log "
        "rather than resuming deletion."
    )
    assert defect_log.stat().st_size > 2_000, (
        "docs/INCIDENT_HISTORY.md is suspiciously small — it is append-only and is "
        "never trimmed, so it should only ever grow."
    )
    assert "INCIDENT_HISTORY.md" in work_md.read_text(), (
        "docs/WORK.md no longer points at docs/INCIDENT_HISTORY.md. A session "
        "pruning the backlog will not find the destination and will fall "
        "back to deleting, which is the behaviour this pair of tests exists "
        "to stop."
    )
# relevance ordering: unfinished on top, finished collapsed, rot never hidden
# --------------------------------------------------------------------------

def _phase_with(verdict_results, recorded="DONE AND LIVE", title="t", id_="x"):
    p = sb.PhaseView(id=id_, title=title, summary="s", recorded=recorded,
                     confidence="high")
    p.results = verdict_results
    return p


def test_settled_phase_is_confirmed_and_recorded_done_and_live():
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    assert p.verdict == "CONFIRMED"
    assert sb._is_settled(p) is True


def test_confirmed_but_partial_phase_is_not_settled():
    """CONFIRMED only means the checkable rules hold, not that there is no
    work left. A phase still recorded as PARTIAL (or OPEN, NOT STARTED, ...)
    must stay in the visible list even when every rule it does carry passes."""
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")], recorded="PARTIAL")
    assert p.verdict == "CONFIRMED"
    assert sb._is_settled(p) is False


def test_contradicted_phase_is_never_settled():
    p = _phase_with([sb.RuleResult("file_exists", sb.FAIL, "")])
    assert p.verdict == "CONTRADICTED"
    assert sb._is_settled(p) is False


def test_render_collapses_only_settled_phases_and_never_collapses_contradicted(tmp_path):
    """Pins the actual page structure: a closed <details class="finished">
    holds only the fully-verified, recorded-done phases; a CONTRADICTED
    phase's row must never appear inside it, and must render outside any
    <details> at all."""
    done = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")],
                        recorded="DONE AND LIVE", title="Finished thing", id_="done")
    partial = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")],
                           recorded="PARTIAL", title="Partial thing", id_="partial")
    rotten = _phase_with([sb.RuleResult("file_exists", sb.FAIL, "")],
                          recorded="DONE AND LIVE", title="Rotten thing", id_="rotten")
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.5,
             "sessions_today": 3, "box_sha": "abc", "main_sha": "abc"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([done, partial, rotten], state, template)

    # No <details> block ever contains the contradicted phase's title.
    details_start = out.index('<details class="finished">')
    details_end = out.index("</details>", details_start)
    collapsed_chunk = out[details_start:details_end]
    assert "Rotten thing" not in collapsed_chunk
    assert "Finished thing" in collapsed_chunk
    assert "Partial thing" not in collapsed_chunk

    # The summary reports exactly one settled phase, and <details> is closed
    # by default (no `open` attribute).
    assert "1 finished and verified" in out
    assert "<details class=\"finished\" open>" not in out
    assert "<details open class=\"finished\">" not in out

    # Rotten and partial both render before the <details> block (attention
    # section), and the contradicted one appears first among them.
    attention_chunk = out[:details_start]
    assert "Rotten thing" in attention_chunk
    assert "Partial thing" in attention_chunk
    assert attention_chunk.index("Rotten thing") < attention_chunk.index("Partial thing")


# --------------------------------------------------------------------------
# freshness is a fact check, not a clock
# --------------------------------------------------------------------------
#
# The board rebuilds on change, not on a schedule, so a page's age proves
# nothing on its own — a quiet weekend legitimately leaves it old with
# nothing wrong. What actually means the system moved on since this page was
# built is the one fact `scripts/status_board.py` already computes for
# itself: the commit the box was running (`box_sha`). This module's job is
# to stamp that fact into the page, in full, unfilled by any guess. The
# server-side comparison against a freshly-read live commit — the part that
# decides whether a banner is shown — lives in `src/api/server.py` and is
# exercised there.

def test_render_stamps_the_untruncated_sha_for_machine_comparison():
    """The machine-readable stamp must carry the FULL commit, not the 9-char
    prefix used for the human-facing footer. A short prefix is a needless
    collision risk for an equality check with nothing else moderating it."""
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    full = "abc1234567890abc1234567890abc1234567890"
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.5,
             "sessions_today": 3, "box_sha": full[:9], "box_sha_full": full,
             "main_sha": full[:9]}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([p], state, template)
    assert f'content="{full}"' in out, "the stamp must hold the untruncated SHA"
    # And the footer keeps showing the short form for humans — unrelated to
    # the machine stamp, must not regress alongside it.
    assert full[:9] in out


def test_render_leaves_the_stamp_empty_when_the_box_sha_is_unreadable():
    """No commit could be read at build time: the stamp must be empty, never
    a fabricated value. Empty is what src/api/server.py treats as "no
    stamp" -> reported as UNKNOWN, never as a silent match."""
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    state = {"in_sync": None, "circuit": None, "spend_today": None,
             "sessions_today": None, "box_sha": None, "box_sha_full": None,
             "main_sha": None}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([p], state, template)
    assert 'name="qamc-board-built-sha" content=""' in out


def test_stale_banner_mechanism_is_gone():
    """The time-based staleness banner (a fixed hour threshold, an inline
    script computing page age in the reader's browser) is removed entirely,
    not merely disabled. Freshness is decided server-side, from a fact."""
    assert not hasattr(sb, "_staleness_banner")
    assert not hasattr(sb, "STALE_AFTER_HOURS")
    assert not hasattr(sb, "_age_words")
    template = (Path(__file__).resolve().parents[1]
                / "scripts" / "status_board_template.html").read_text()
    assert "{{STALE_BANNER}}" not in template
    assert "id=\"stale\"" not in template


# --------------------------------------------------------------------------
# the serve-time freshness banner (src/api/server.py)
# --------------------------------------------------------------------------
#
# `_freshness_banner` is the actual "does the record still hold" decision:
# it runs on every request, comparing the commit this page was built from
# against the commit the box is running right now. Pure string-in,
# string-out — no server, no filesystem — so it is pinned directly here.

def test_matching_built_and_live_sha_produce_no_banner():
    sha = "abc1234567890abc1234567890abc1234567890"
    assert _freshness_banner(sha, sha) == ""


def test_differing_built_and_live_sha_produce_a_banner_that_says_so():
    built = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
    live = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
    out = _freshness_banner(built, live)
    assert out, "a version mismatch must produce a visible banner"
    assert "out of date" in out.lower() or "changed" in out.lower()
    assert built[:9] in out
    assert live[:9] in out
    # Reuses the page's existing crit-colour class; no new colour introduced.
    assert 'class="stale"' in out


@pytest.mark.parametrize("built,live", [(None, "a" * 40), ("a" * 40, None), (None, None)])
def test_undeterminable_version_reports_unknown_not_a_false_all_clear(built, live):
    """Either side missing must say UNKNOWN plainly — never silence (which a
    reader takes as "fine") and never a claim it cannot back up."""
    out = _freshness_banner(built, live)
    assert out != "", "an undeterminable version must not read as a clean bill of health"
    assert "unknown" in out.lower()
    assert 'class="stale"' in out


# --------------------------------------------------------------------------
# is this summary written for him, or for a developer?
# --------------------------------------------------------------------------
#
# The owner reads this board and nothing else, and is not a developer.
# `plain_summary` is supposed to be written for him, but it lives inside an
# engineering document engineering agents maintain, so it drifts back toward
# PR numbers and file paths the moment the next agent writes one. Blocking
# on a wordlist of "technical-sounding" words was tried and rejected: it
# produces jargon-free prose that is still useless to him, not good prose.
# What ships instead detects the MECHANICAL SHAPE of engineering text (a
# path, a reference number, a hash, a code identifier) and reports it
# without blocking anything — see `summary_is_engineer_facing`.
#
# The failure mode that would make this feature ignored is a false positive
# on ordinary English, so that gets tested for deliberately, not just the
# positive cases.

def test_plain_english_summaries_are_never_flagged():
    """Genuinely plain-English summaries, including ones that use ordinary
    slash and parenthesis constructions a naive detector could trip on."""
    plain = [
        "The system now checks stop-losses before every trade and blocks "
        "anything too risky.",
        "We fixed the bug where the desk sold winners too early. It now "
        "holds until the target price.",
        "The cost/benefit of each trade is weighed against the risk/reward "
        "before it is sized.",
        "Reported 3/15/2026 as the date the change went live, twenty-six "
        "checks passed in a row.",
    ]
    for summary in plain:
        flagged, reason = sb.summary_is_engineer_facing(summary)
        assert flagged is False, f"false positive on plain English: {summary!r}"
        assert reason == ""
        assert sb.summary_engineering_markers(summary) == []


def test_missing_or_empty_summary_is_flagged_with_its_own_reason():
    """Silence is not neutral: nobody wrote a plain-English description, and
    that gets a distinct reason from "wrote one but it's jargon"."""
    for summary in ("", "   ", None):
        flagged, reason = sb.summary_is_engineer_facing(summary)
        assert flagged is True
        assert "no plain-english description" in reason.lower()


@pytest.mark.parametrize("summary,expected_marker", [
    ("See docs/phases.yaml for the manifest.", "a file path"),
    ("STATE.md still names a commit three deploys behind.", "a file path"),
    ("The backtester lives in src/backtest/ and has its own CLI.", "a file path"),
    ("Confirmed via sudo -n -u qamc git -C /home/qamc/quant-agent log "
     "--oneline -1", "a file path"),
    ("Fixes issue #42 where stops did not trail correctly.", "a PR or issue number"),
    ("Landed in PR #150 and deployed the same day.", "a PR or issue number"),
    ("Commit a1b2c3d fixed the regression.", "a commit hash"),
    ("The `event_risk` field is now populated from real data.", "a code identifier"),
    ("afternoon_reserve_pct now walls off part of the budget.", "a code identifier"),
    ("refresh_openrouter_pricing() is only called from two places.", "a code identifier"),
])
def test_each_marker_type_is_detected(summary, expected_marker):
    flagged, reason = sb.summary_is_engineer_facing(summary)
    assert flagged is True, f"expected a flag on: {summary!r}"
    assert expected_marker in sb.summary_engineering_markers(summary)
    assert reason != ""


def test_ordinary_pluralisation_is_not_read_as_a_function_call():
    """"trade(s)" is ordinary English shorthand, not `word(...)` call syntax
    — the parenthesis check must not treat every (s)/(es) as code."""
    flagged, _ = sb.summary_is_engineer_facing(
        "Every open trade(s) now carries its own stop-loss.")
    assert flagged is False


def test_a_bare_directory_word_pair_is_not_a_path():
    """"data/info" and similar two-word slash pairings are ordinary English
    shorthand, not a path — a path check anchored only on known directory
    names would still catch this without a second path segment or an
    extension, so this pins that it doesn't."""
    flagged, _ = sb.summary_is_engineer_facing(
        "The data/info from the news feed is combined before deciding.")
    assert flagged is False


def test_render_shows_a_top_of_page_count_when_something_is_flagged():
    """The count belongs near the top, in the same region as the freshness
    banner, so he doesn't have to hunt the list for it — not buried in a
    per-item marker he might not scroll to."""
    flagged_summary = "See docs/phases.yaml for the manifest."
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    p.summary = flagged_summary
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.5,
             "sessions_today": 3, "box_sha": "abc", "main_sha": "abc"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([p], state, template)
    # Search for the rendered element (`class="jargon-banner"`), not the CSS
    # rule (`.jargon-banner{...}`), which sits earlier in <head> and would
    # make this pass without the banner actually being in the page body.
    banner_idx = out.index('class="jargon-banner"')
    header_idx = out.index('<header class="mast">')
    assert banner_idx < header_idx, "the count must appear before the page header"
    assert "1 description" in out


def test_render_shows_no_jargon_banner_when_nothing_is_flagged():
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    p.summary = "A plain-English summary with nothing mechanical in it."
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.5,
             "sessions_today": 3, "box_sha": "abc", "main_sha": "abc"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([p], state, template)
    # The CSS rule itself is always present (it's part of the static
    # template); what must be absent is the rendered element.
    assert 'class="jargon-banner"' not in out


def test_flagged_summary_still_renders_in_full_underneath_the_marker():
    """The board reports; it does not hide or strip anything. An unreadable
    description is still more useful to him than no description."""
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    p.summary = "See docs/phases.yaml and PR #150 for the detail."
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.5,
             "sessions_today": 3, "box_sha": "abc", "main_sha": "abc"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render([p], state, template)
    assert "jargon-flag" in out
    assert "See docs/phases.yaml and PR #150 for the detail." in out


def test_flagging_a_summary_never_changes_the_phase_verdict():
    """This feature reports on prose quality; it must never touch what the
    board CHECKS. A jargon-heavy summary on a phase with passing evidence
    still reads CONFIRMED, and the exit code (see main()) is untouched by
    it — only a real contradiction moves that."""
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")])
    p.summary = "See docs/phases.yaml and PR #150 for the detail."
    assert p.verdict == "CONFIRMED"
    assert p.summary_flagged is True


def test_no_pending_decision_is_overdue():
    """A deferred decision must expire loudly, not quietly.

    On 2026-08-28 `docs/WORK.md` said of the reward:risk floor: "gather a week
    of these rejections first, then decide which of the two numbers is wrong."
    Nobody came back to it. On 2026-09-01 the desk reviewed 38 qualified
    signals and placed zero trades for precisely that reason, and the owner
    pointed out — correctly — that we were re-deriving a conclusion the repo
    had already reached and forgotten.

    A promise to remember is not a mechanism. This is the mechanism: any line
    matching `- [ ] DECIDE BY YYYY-MM-DD — ...` fails the build once that date
    has passed, so an unmade decision becomes a red build rather than a quiet
    omission.

    Deleting the line to go green is the one forbidden fix. Decide it, record
    the decision, and remove the line in the same commit.
    """
    import datetime as _dt
    import re as _re

    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    if not work_md.exists():
        return

    pattern = _re.compile(r"^- \[ \] DECIDE BY (\d{4})-(\d{2})-(\d{2}) [-—] (.+)$")
    today = _dt.date.today()
    overdue = []
    for line in work_md.read_text().splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        y, mo, d, question = m.groups()
        try:
            due = _dt.date(int(y), int(mo), int(d))
        except ValueError:  # a malformed date is itself a defect
            overdue.append(f"unparseable date in: {line.strip()[:100]}")
            continue
        if due < today:
            overdue.append(f"{due} ({(today - due).days}d overdue) — {question[:90]}")

    assert not overdue, (
        "docs/WORK.md has overdue pending decisions:\n  "
        + "\n  ".join(overdue)
        + "\n\nDecide them and remove the line in the same commit that records "
          "the decision. Do NOT delete the line to make this pass."
    )


# ---------------------------------------------------------------------------
# The funnel queue on the board.
#
# The owner asked whether the ranked work queue was visible on the dashboard
# he already has, "automatically". It now is — but only for as long as the
# board can still parse `docs/WORK.md`. These tests exist because the failure
# mode is silent: a heading rename would render an empty section that looks
# exactly like "no work outstanding", which is the most misleading thing this
# page could say.
# ---------------------------------------------------------------------------

def test_the_real_backlog_still_parses():
    """The shipped docs/WORK.md must actually yield the queue.

    Not a synthetic fixture — the real file, because the thing that breaks is
    the real file being edited into a shape the parser no longer recognises.
    """
    work = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    items, problem = sb.load_funnel_queue(work)
    assert problem is None, problem
    assert len(items) >= 10, f"only {len(items)} queue items parsed"
    assert [i.rank for i in items] == sorted(i.rank for i in items)
    top = items[0]
    assert top.rank == 1
    assert "reward:risk" in top.title.lower()
    assert top.classification == "TOO STRICT"
    assert top.pct == 25


def test_a_renamed_heading_says_so_instead_of_rendering_empty(tmp_path):
    """A shape change must be LOUD. An empty queue section reads as 'nothing
    to do', which is the opposite of the truth it would be hiding."""
    p = tmp_path / "WORK.md"
    p.write_text("# Work\n\n## Some Other Heading\n\n**1. A thing — 1 of 2 (50%). DEFECT.**\n")
    items, problem = sb.load_funnel_queue(p)
    assert items == []
    assert problem and "could not be read" in problem
    assert "Queue unavailable" in sb._render_queue(items, problem)


def test_a_missing_backlog_file_says_so(tmp_path):
    items, problem = sb.load_funnel_queue(tmp_path / "nope.md")
    assert items == []
    assert problem and "missing" in problem


def test_heading_present_but_items_unparseable_is_reported(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text("## THE FUNNEL QUEUE — x\n\nprose only, no numbered items\n")
    items, problem = sb.load_funnel_queue(p)
    assert items == []
    assert problem and "shape has changed" in problem


def test_classification_is_carried_by_the_word_not_only_colour():
    """The owner is red/green colour blind. Every status must be legible with
    all colour stripped out, so the label text itself has to be in the markup."""
    items = [
        sb.QueueItem(1, "A blocked thing", "TOO STRICT", "17 of 68 (25%)", 25, False),
        sb.QueueItem(2, "A broken thing", "DEFECT", "2 of 68 (3%)", 3, False),
    ]
    html_out = sb._render_queue(items, None)
    text_only = re.sub(r"<[^>]+>", " ", html_out)
    assert "too strict" in text_only
    assert "defect" in text_only


def test_a_finished_item_reads_as_done():
    done = sb.QueueItem(1, "Fixed thing", "DEFECT", "", None, True)
    assert done.state == "done"
    assert "line-through" in sb._render_queue([done], None)


def test_pending_decisions_show_time_remaining_and_overdue(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text(
        "- [ ] DECIDE BY 2026-09-09 — Level quality bar\n"
        "- [ ] DECIDE BY 2026-08-01 — Something long forgotten\n"
    )
    got = sb.load_pending_decisions(p, today=dt.date(2026, 9, 2))
    assert [d.due for d in got] == [dt.date(2026, 8, 1), dt.date(2026, 9, 9)]
    assert got[0].overdue and got[0].days_left == -32
    assert not got[1].overdue and got[1].days_left == 7
    out = sb._render_decisions(got)
    assert "32 days overdue" in out and "7 days left" in out


def test_the_board_and_the_build_read_one_decision_format():
    """`test_no_pending_decision_is_overdue` and the board must never disagree
    about what a pending decision looks like — one format, one regex shape."""
    line = "- [ ] DECIDE BY 2026-09-16 — Which model runs the seat?"
    build_re = re.compile(r"^- \[ \] DECIDE BY (\d{4})-(\d{2})-(\d{2}) [-—] (.+)$")
    assert build_re.match(line)
    assert sb._DECISION_RE.match(line)


def test_nothing_waiting_says_so_rather_than_showing_a_blank(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text("no decisions here\n")
    assert "Nothing is waiting on you" in sb._render_decisions(sb.load_pending_decisions(p))


# ---------------------------------------------------------------------------
# an item cannot be allowed to disagree with its own title
#
# Three real items said FIXED/MERGED in their own title while the board
# still showed them as open, because striking a title through (the mechanism
# that already existed) was a remembered step, never an enforced one. This
# pins the check that replaces the memory with a build failure.
# ---------------------------------------------------------------------------

def test_the_real_backlog_has_no_item_contradicting_its_own_title():
    """The real docs/WORK.md, not a fixture — the failure mode is real items
    drifting out of sync with their own `~~done~~` marker over time."""
    work = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    flagged = sb.find_closed_items_not_marked_done(work)
    assert not flagged, (
        "these backlog items claim to be finished in their own title but are "
        "not struck through, so the status board still shows them as open "
        "work:\n  " + "\n  ".join(flagged) +
        "\n\nEither wrap the title in ~~...~~ (it is actually done) or "
        "reword the title so it no longer claims a closure it hasn't reached."
    )


def test_a_title_claiming_closure_without_strikethrough_is_flagged(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text(
        "## THE FUNNEL QUEUE\n\n"
        "**1. Real bug — FIXED 2026-09-04.**\n\n"
        "**2. Another one — MERGED, PR #999.**\n"
    )
    flagged = sb.find_closed_items_not_marked_done(p)
    assert len(flagged) == 2
    assert "item 1" in flagged[0]
    assert "item 2" in flagged[1]


def test_a_struck_through_title_is_not_flagged(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text(
        "## THE FUNNEL QUEUE\n\n"
        "**~~1. Real bug — FIXED 2026-09-04.~~**\n"
    )
    assert sb.find_closed_items_not_marked_done(p) == []


def test_a_partial_or_pending_closure_is_not_flagged():
    """"MOSTLY FIXED, one real judgment call left" and "FIXED, pending
    review" are honest about not being finished yet — they must stay open,
    not get swept into a false-done state just because they contain a
    closure word."""
    p_partial = "**1. Thing — MOSTLY FIXED, one real judgment call left.**"
    p_pending = "**2. Other thing — FIXED, pending review.**"
    for line in (p_partial, p_pending):
        text = f"## THE FUNNEL QUEUE\n\n{line}\n"
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False,
        ) as f:
            f.write(text)
            path = Path(f.name)
        try:
            assert sb.find_closed_items_not_marked_done(path) == [], line
        finally:
            path.unlink()


def test_a_missing_backlog_or_heading_flags_nothing(tmp_path):
    assert sb.find_closed_items_not_marked_done(tmp_path / "nope.md") == []
    p = tmp_path / "WORK.md"
    p.write_text("# Work\n\nno funnel queue heading here\n")
    assert sb.find_closed_items_not_marked_done(p) == []


# ---------------------------------------------------------------------------
# the PM test gate: "garbage in, garbage out" as its own board section
#
# The owner's own repeated framing: the PM model-choice test cannot mean
# anything until everything feeding the PM is clean. That framing lived only
# as prose scattered across the data-quality audit and the PM-input-
# architecture note in docs/WORK.md — neither of which the board renders at
# all. This is a curated index into that same material, in the one shape
# (`**N. Title — status.**`) the board already knows how to render, so the
# owner can find "what's blocking the PM test" as its own line items instead
# of hunting through paragraphs.
# ---------------------------------------------------------------------------

def test_the_real_pm_gate_parses_and_has_at_least_one_open_item():
    """The shipped docs/WORK.md must actually yield the gate. Not a
    synthetic fixture — the real file, same reasoning as the funnel-queue
    equivalent: the thing that breaks is the real file being edited into a
    shape the parser no longer recognises."""
    work = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    items, problem = sb.load_pm_gate(work)
    assert problem is None, problem
    assert len(items) >= 5, f"only {len(items)} PM-gate items parsed"
    open_items = [i for i in items if not i.done]
    assert open_items, "the gate reports nothing open — that would mean the PM test is unblocked"


def test_a_renamed_pm_gate_heading_says_so_instead_of_rendering_empty(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text("# Work\n\n## Some Other Heading\n\n**1. A thing — FIXED.**\n")
    items, problem = sb.load_pm_gate(p)
    assert items == []
    assert problem and "could not be read" in problem


def test_a_missing_backlog_file_says_so_for_the_pm_gate(tmp_path):
    items, problem = sb.load_pm_gate(tmp_path / "nope.md")
    assert items == []
    assert problem and "missing" in problem


def test_pm_gate_stops_at_its_own_end_marker_not_the_rest_of_the_file(tmp_path):
    """Without an explicit stop marker, the gate would swallow every heading-
    free paragraph after it — including unrelated backlog content that just
    happens to share the same `##`-free run of text."""
    p = tmp_path / "WORK.md"
    p.write_text(
        "## PM TEST GATE\n\n"
        "**1. Seat one — FIXED.**\n"
        "**2. Seat two — OPEN.**\n\n"
        "<!-- END PM TEST GATE -->\n\n"
        "**3. Unrelated later item — FIXED.**\n"
    )
    items, problem = sb.load_pm_gate(p)
    assert problem is None
    assert [i.rank for i in items] == [1, 2]


def test_pm_gate_items_do_not_leak_into_the_funnel_queue_or_vice_versa(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text(
        "## PM TEST GATE\n\n"
        "**1. Gate item — OPEN.**\n\n"
        "<!-- END PM TEST GATE -->\n\n"
        "## THE FUNNEL QUEUE\n\n"
        "**1. Queue item — DEFECT.**\n"
    )
    gate_items, _ = sb.load_pm_gate(p)
    queue_items, _ = sb.load_funnel_queue(p)
    assert [i.title for i in gate_items] == ["Gate item"]
    assert [i.title for i in queue_items] == ["Queue item"]
