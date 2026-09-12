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
import html as html_mod
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
                "{{RULES_UNKNOWN}}", "{{BOX_SHA}}", "{{MAIN_SHA}}", "{{JARGON_BANNER}}",
                "{{RIGHT_NOW}}", "{{DECISIONS}}", "{{QUEUE}}", "{{PAUSED}}",
                "{{FINISHED_UNMARKED}}", "{{FINISHED_UNMARKED_COUNT}}",
                "{{REVIEW_OWED}}", "{{REVIEW_OWED_COUNT}}",
                "{{RESOLVED}}", "{{QUEUE_OPEN}}",
                "{{QUEUE_TOTAL}}", "{{PAUSED_COUNT}}", "{{RESOLVED_COUNT}}",
                "{{UNEXPLAINED_NOTE}}", "{{PM_GATE}}",
                "{{PM_GATE_DONE}}", "{{PM_GATE_OPEN}}", "{{PM_GATE_TOTAL}}"):
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
    rendered = sb._render_open_queue(items, problem)
    assert "could not be read" in rendered
    # And it must not read as an empty backlog.
    assert "Nothing is queued" not in rendered


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
    html_out = sb._render_open_queue(items, None)
    text_only = re.sub(r"<[^>]+>", " ", html_out)
    assert "too strict" in text_only
    assert "defect" in text_only


def test_a_finished_item_reads_as_done():
    done = sb.QueueItem(1, "Fixed thing", "DEFECT", "", None, True)
    assert done.state == "done"
    assert done.bucket == "resolved"
    # Struck through in the resolved list — and the strike-through is on top
    # of the words "Already resolved" in the template, never instead of them.
    assert "ol-done" in sb._render_one_liners([done], "nothing", struck=True)


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


# ---------------------------------------------------------------------------
# The plain-language convention
#
# These pin the property the whole rewrite rests on: prose the owner reads is
# WRITTEN, in the backlog, by a person — and where nobody has written it, the
# page says so. It is never invented, never summarised out of the engineering
# notes, and never dropped. An invented explanation would be the same rot this
# board exists to catch, wearing a friendlier face.
# ---------------------------------------------------------------------------

# The item itself — number, title, status, engineering notes — is still
# docs/WORK.md's shape. Its prose has moved out to a separate fixture below,
# standing in for docs/BOARD_NOTES.md, keyed to the item by "## item 1"
# rather than living inside the item's own body.
_FULL_ITEM = (
    "## THE FUNNEL QUEUE\n\n"
    "**1. The reward:risk floor — 17 of 68 (25%). TOO STRICT.**\n\n"
    "Engineering detail nobody should have to read: the ATR floor overwrites\n"
    "the structural stop, and this paragraph mentions that it was fixed.\n"
)

_FULL_ITEM_NOTES = (
    "## item 1\n\n"
    "**Plain language —** The desk refuses a trade unless the likely gain is\n"
    "at least one and a half times what it is risking.\n"
    "**Example —** You want to buy at $100 with a stop at $98 and a target at\n"
    "$105: risking $2 to make $5. The desk moves the stop to $95 on its own,\n"
    "recalculates it as risking $5 to make $5, and refuses the trade.\n"
    "**The decision —** Should a stop you can point at on the chart always be\n"
    "honoured, however tight it is?\n"
    "**Recommendation —** Yes. Pad the stop only when there is no real level\n"
    "to put it at.\n\n"
    "A stray unlabelled paragraph, mentioning the ATR floor, that must not be\n"
    "swallowed into the recommendation above it just because it sits in the\n"
    "same notes block.\n"
)


def _notes(tmp_path, text):
    """Write `text` as a `docs/BOARD_NOTES.md`-shaped fixture and parse it,
    the same way `render` parses the real file."""
    p = tmp_path / "BOARD_NOTES.md"
    p.write_text(text)
    return sb.load_board_notes(p)


def test_an_item_carries_its_plain_language_example_and_recommendation(tmp_path):
    notes = _notes(tmp_path, _FULL_ITEM_NOTES)
    items = sb._parse_numbered_items(_FULL_ITEM.split("## THE FUNNEL QUEUE")[1],
                                     notes=notes)
    assert len(items) == 1
    p = items[0].prose
    assert p.plain.startswith("The desk refuses a trade")
    assert "$100" in p.example and "$95" in p.example
    assert p.decision.startswith("Should a stop")
    assert p.recommendation.startswith("Yes.")


def test_a_blank_line_ends_a_block_so_engineering_prose_is_not_swallowed(tmp_path):
    """The paragraph after the blank line in docs/BOARD_NOTES.md is ordinary
    commentary, not a labelled field. If it leaked into the recommendation
    the owner would be shown text nobody wrote as one."""
    notes = _notes(tmp_path, _FULL_ITEM_NOTES)
    items = sb._parse_numbered_items(_FULL_ITEM.split("## THE FUNNEL QUEUE")[1],
                                     notes=notes)
    assert "ATR floor" not in items[0].prose.recommendation
    assert "ATR floor" not in items[0].prose.plain


def test_wrapped_prose_lines_are_joined_not_truncated(tmp_path):
    notes = _notes(tmp_path, _FULL_ITEM_NOTES)
    items = sb._parse_numbered_items(_FULL_ITEM.split("## THE FUNNEL QUEUE")[1],
                                     notes=notes)
    # The second physical line of the plain-language block must be present.
    assert "one and a half times" in items[0].prose.plain


def test_prose_no_longer_comes_from_work_mds_own_body():
    """The relocation's core guarantee: a plain-language block typed straight
    into a WORK.md item's body must NOT reach the page — only a matching
    heading in docs/BOARD_NOTES.md does. Without that, this file's own cap
    would be pointless: the prose it was moved to avoid could just come back
    in through the body text instead."""
    body = ("**3. A thing — DEFECT.**\n\n"
            "**Plain language —** this text is typed into the wrong file "
            "now.\n")
    items = sb._parse_numbered_items(body)  # no notes: nothing to look up
    assert items[0].prose.plain == ""
    assert not items[0].prose.has_any
    # Still visible, but as raw engineering notes, never as plain language.
    assert "wrong file" in items[0].raw_body


# ---------------------------------------------------------------------------
# docs/BOARD_NOTES.md — the prose file itself
#
# The key property this file's whole design rests on: an entry is found by
# the item's NUMBER and SECTION, never by its title, so a rename in
# docs/WORK.md can never silently orphan the note written for it.
# ---------------------------------------------------------------------------

def test_board_notes_keys_by_number_and_section_not_title(tmp_path):
    notes = _notes(tmp_path, (
        "## item 7\n\n"
        "**Plain language —** it is a queue thing.\n\n"
        "## gate item 3\n\n"
        "**Plain language —** it is a gate thing.\n\n"
        "## decision due 2026-09-16\n\n"
        "**Recommendation —** Yes.\n"
    ))
    assert notes["item 7"].plain == "it is a queue thing."
    assert notes["gate item 3"].plain == "it is a gate thing."
    assert notes["decision due 2026-09-16"].recommendation == "Yes."
    # The funnel queue and the gate both number from 1: "item 3" and "gate
    # item 3" must never collapse onto the same entry.
    assert "item 3" not in notes


def test_board_notes_heading_matches_regardless_of_case_or_spacing(tmp_path):
    notes = _notes(tmp_path, "###   ITEM   9\n\n**Plain language —** ok.\n")
    assert notes["item 9"].plain == "ok."


def test_board_notes_missing_file_is_empty_not_an_error(tmp_path):
    assert sb.load_board_notes(tmp_path / "nope.md") == {}


def test_board_notes_with_no_recognised_heading_is_empty(tmp_path):
    p = tmp_path / "BOARD_NOTES.md"
    p.write_text("Just a header and some prose, no heading it can key on.\n")
    assert sb.load_board_notes(p) == {}


def test_an_item_with_no_matching_note_is_unexplained_not_borrowed(tmp_path):
    """The whole point of keying by number: an item docs/BOARD_NOTES.md has
    never heard of must render as unexplained, never silently inherit
    prose written for a different item."""
    notes = _notes(tmp_path, "## item 7\n\n**Plain language —** for item 7 only.\n")
    items = sb._parse_numbered_items("**9. Something else — DEFECT.**\n", notes=notes)
    assert not items[0].prose.has_any


def test_the_real_board_notes_file_loads_without_error():
    """docs/BOARD_NOTES.md ships in the repo; whatever it currently holds
    must parse without raising, exactly like the real backlog."""
    path = Path(__file__).resolve().parents[1] / "docs" / "BOARD_NOTES.md"
    assert path.exists()
    notes = sb.load_board_notes(path)
    assert isinstance(notes, dict)


def test_render_reads_prose_from_board_notes_not_work_md(tmp_path):
    """End to end: `render` must take its prose from `board_notes`, not from
    anything typed into the `work_md` item's own body."""
    work = tmp_path / "WORK.md"
    work.write_text(
        "## THE FUNNEL QUEUE\n\n"
        "**1. A real thing — 1 of 2 (50%). DEFECT.**\n\n"
        "**Plain language —** typed into the wrong file, must not render.\n"
    )
    notes = tmp_path / "BOARD_NOTES.md"
    notes.write_text(
        "## item 1\n\n"
        "**Plain language —** the desk explanation lives here now.\n"
    )
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.1,
             "sessions_today": 1, "box_sha": "abc", "main_sha": "abc"}
    template = (Path(__file__).resolve().parents[1] / "scripts"
                / "status_board_template.html")
    out = sb.render(phases, state, template, work_md=work, board_notes=notes)
    assert "the desk explanation lives here now" in out
    assert "typed into the wrong file, must not render" not in out


@pytest.mark.parametrize("line,field", [
    ("**Plain language —** a", "plain"),
    ("Plain English: a", "plain"),
    ("  **Example -** a", "example"),
    ("DECISION — a", "decision"),
    ("**My recommendation —** a", "recommendation"),
])
def test_every_accepted_label_spelling_parses(line, field):
    """The labels are typed by hand. A near-miss spelling must land in the
    right block rather than being silently ignored, because silently ignored
    means the owner is told nobody wrote it when somebody did."""
    assert getattr(sb.parse_prose([line]), field) == "a"


def test_markdown_never_reaches_the_page_as_literal_characters():
    got = sb.parse_prose(["Plain language: the **stop** sits at `entry - 2*atr`"])
    assert "**" not in got.plain
    assert "`" not in got.plain
    assert "stop" in got.plain and "entry - 2*atr" in got.plain


def test_an_item_with_no_prose_renders_honestly_and_is_not_dropped():
    """The honest outcome, and the one that must never regress into invention:
    the item is still on the page, and the page says nobody has explained it."""
    bare = sb.QueueItem(7, "Some unexplained thing", "DEFECT", "", None, False)
    out = sb._render_open_queue([bare], None)
    assert "Some unexplained thing" in out          # not dropped
    assert "Nobody has written" in out              # stated, not papered over
    assert "no plain-English version yet" in out    # and flagged on the line


def test_nothing_is_invented_for_an_unexplained_item():
    """Whatever the page says about an unexplained item, it must not contain
    an invented explanation.

    The backlog's own engineering notes ARE now shown — contained, collapsed
    and labelled as engineering notes — because hiding them left the top card
    with nothing on it at all. What must never appear is a sentence nobody
    wrote: so every word of the notes shown here has to be a verbatim slice of
    the source, and none of it may be laid out as a plain-language block.
    """
    body = ("## THE FUNNEL QUEUE\n\n"
            "**3. A thing — DEFECT.**\n\n"
            "The real cause is a recursion fault in the bar fetch, traced to\n"
            "a delisted warrant reaching the data layer.\n")
    items = sb._parse_numbered_items(body.split("## THE FUNNEL QUEUE")[1])
    out = sb._render_open_queue(items, None)
    assert not items[0].prose.has_any

    # The notes appear verbatim, and ONLY inside the marked container.
    assert "recursion fault in the bar fetch" in items[0].raw_body
    assert 'class="raw"' in out
    assert "Engineering notes from the backlog" in out
    raw_section = out.split('<details class="raw">', 1)[1]
    assert "recursion" in raw_section
    assert "delisted" in raw_section
    # ...and never dressed up as the explanation somebody did not write.
    before_raw = out.split('<details class="raw">', 1)[0]
    assert "recursion" not in before_raw
    assert "pb-plain" not in out
    assert "pb-eg" not in out


@pytest.mark.parametrize("raw,expect_in,expect_out", [
    # Single-asterisk emphasis, which the real backlog uses and which used to
    # reach the page as two stray asterisks inside the notes block.
    ("*Owner clarification, 2026-09-10: not re-measured yet.*",
     "Owner clarification", "*"),
    # Arithmetic must survive verbatim. A lone asterisk between two word
    # characters is multiplication, not emphasis.
    ("the stop sits at entry - 2*atr on every fill", "2*atr", None),
])
def test_stray_markdown_never_reaches_the_notes_block(raw, expect_in, expect_out):
    out = sb._render_prose(sb.Prose(), raw_source=sb._strip_markdown(raw))
    assert expect_in in out
    if expect_out:
        assert expect_out not in out.split('class="raw"', 1)[1]


def test_the_missing_prose_fallback_says_so_before_showing_raw_notes():
    """The shape the owner asked for, in order: the honest sentence first, the
    raw source second and clearly secondary. A card with no prose must never
    read as though the engineering notes were the explanation."""
    out = sb._render_prose(sb.Prose(), raw_source="Some engineering notes.")
    assert out.index("No plain-English version yet") < out.index('class="raw"')
    assert "Nobody has written this one up for you yet" in out
    assert "not an explanation" in out


def test_the_fallback_is_clean_when_there_is_no_raw_source_either():
    """Nothing to show is not the same as something broken. With no prose AND
    no source text, the card is one honest sentence and no empty container."""
    out = sb._render_prose(sb.Prose(), raw_source="")
    assert "No plain-English version yet" in out
    assert 'class="raw"' not in out
    assert "<p></p>" not in out


def test_a_long_raw_body_is_cut_and_says_that_it_was_cut():
    """Contained means contained. A 4,000-character engineering note pasted
    whole under the top card is the structural mess by another route."""
    long_note = "word " * 900
    out = sb._render_prose(sb.Prose(), raw_source=long_note)
    assert "Shortened here" in out
    assert len(re.sub(r"<[^>]+>", "", out)) < 1600


def test_the_top_card_with_no_prose_is_compact_and_contained(tmp_path):
    """Complaint 2, end to end: the prominent card for an item nobody has
    written up is short, says why, and keeps the raw notes behind a label."""
    it = sb.QueueItem(
        7, "A thing with a developer-written name", "DEFECT", "", None, False,
        headline="A thing — DEFECT.",
        raw_body="Traced to a recursion fault in `bar_fetch` for a delisted warrant.")
    out = sb._render_right_now([], [], [it])
    assert out.count('class="rn"') == 1
    assert "No plain-English version yet" in out
    # The raw text is present, contained, and behind the label.
    assert 'class="raw"' in out
    assert out.index("No plain-English version yet") < out.index('class="raw"')
    # And the identifier is on the card, so he can quote it.
    assert "item 7" in out


def test_a_long_headline_on_the_top_card_is_stepped_down_not_rewritten():
    """A whole sentence set at display size reads as a mess. The card calms
    the type; it must never invent a shorter title, which would be a second
    name that drifts from the real one."""
    long_title = ("Order-fill detection was a fixed-interval REST poll from "
                  "1992, not the real-time mechanism Alpaca offers")
    it = sb.QueueItem(42, long_title, "", "", None, False)
    out = sb._render_right_now([], [], [it])
    assert "rn-h-long" in out
    assert long_title in out            # shown in full, unshortened

    short = sb.QueueItem(1, "The reward:risk floor", "", "", None, False)
    assert "rn-h-long" not in sb._render_right_now([], [], [short])


def test_prose_written_for_a_developer_is_marked_not_accepted_silently():
    p = sb.parse_prose(["Plain language: see `portfolio_manager.py` and PR #212"])
    assert p.jargon_markers
    out = sb._render_prose(p)
    assert "Written for a developer" in out
    # ...and the words still render. An unreadable description beats none.
    assert "portfolio_manager.py" in out


# ---------------------------------------------------------------------------
# Reading the real item shape
#
# The old parser required the bold to close at end of line, and silently
# dropped every item that did not. Ten live items were invisible to the owner
# for that reason alone.
# ---------------------------------------------------------------------------

def test_body_text_on_the_headline_line_does_not_hide_the_item():
    body = ("\n**41. A persistently broken ticker could fail silently — "
            "FIXED 2026-09-10.** The earlier fix stopped one bad symbol from "
            "crashing the whole scan.\n")
    items = sb._parse_numbered_items(body)
    assert [i.rank for i in items] == [41]
    assert items[0].title.startswith("A persistently broken ticker")


def test_a_headline_wrapped_onto_a_second_line_does_not_hide_the_item():
    body = ("\n**30. The sizing path still owes the same amendment the ranking\n"
            "path just got — FIXED.**\n\nSome body.\n")
    items = sb._parse_numbered_items(body)
    assert [i.rank for i in items] == [30]
    assert "ranking path just got" in items[0].title


def test_body_text_after_the_headline_on_the_same_line_still_lands_in_raw_body():
    """Text on the same physical line as the closing `**` used to be where a
    plain-language block was typed, back when prose lived in this file. It no
    longer is — a `Plain language` line here has no heading to attach it to,
    so it is not treated as prose at all — but it must still be captured as
    engineering body text rather than silently dropped, same as any other
    body content on that line."""
    body = ("\n**5. A thing — DEFECT.** **Plain language —** it is a thing.\n")
    items = sb._parse_numbered_items(body)
    assert items[0].prose.plain == ""
    assert "it is a thing" in items[0].raw_body


def test_the_real_backlog_shows_more_items_than_the_strict_shape_would():
    """Guards the actual regression this fixed. If someone narrows the item
    regex back to the strict shape, the owner's board silently loses items
    again — so assert the widened parser finds strictly more of them."""
    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    items, problem = sb.load_funnel_queue(work_md)
    assert problem is None
    body = work_md.read_text().split(sb._QUEUE_HEADING, 1)[1]
    for stop in ("### Re-measure gate", "\n## ", "\n### "):
        if stop in body:
            body = body.split(stop, 1)[0]
    strict = [l for l in body.splitlines() if sb._QUEUE_ITEM_RE.match(l.strip())]
    assert len(items) > len(strict)


# ---------------------------------------------------------------------------
# The closure check must not regress
# ---------------------------------------------------------------------------

def test_body_prose_saying_fixed_is_not_read_as_the_items_own_claim(tmp_path):
    """The exact historical false positive: an item whose BODY mentions that
    something was fixed is not claiming to be closed itself, and must not fail
    the build. This is why the closure check reads the bold span, never the
    whole physical line."""
    p = tmp_path / "WORK.md"
    p.write_text(
        "## THE FUNNEL QUEUE\n\n"
        "**9. Still very much open — NOT YET DIAGNOSED.** The neighbouring "
        "problem was fixed on Tuesday, which is why this one now shows up.\n"
    )
    assert sb.find_closed_items_not_marked_done(p) == []


def test_a_title_claiming_closure_is_still_flagged_after_the_widening(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text("## THE FUNNEL QUEUE\n\n**4. A thing — FIXED.**\n")
    assert sb.find_closed_items_not_marked_done(p)


def test_the_board_reports_a_self_contradicting_item_to_the_owner_itself():
    """The build check is deliberately narrow. The page is not: an item whose
    own words say finished while the backlog has not struck it off is shown to
    him as FINISHED, with the untidy line stated, rather than being filed as
    live work (which is the déjà vu) or silently as signed off."""
    it = sb.QueueItem(25, "A protected-position rule", "", "", None, False,
                      headline="A protected-position rule — DONE 2026-09-04.")
    assert it.claims_closure is True
    assert it.bucket == "finished_unmarked"
    out = sb._render_finished_unmarked([it])
    assert "finished according to its own note" in out
    assert "not ticked it off" in out
    assert "item 25" in out


def test_a_partial_claim_stays_open_not_contradictory():
    it = sb.QueueItem(2, "A thing", "", "", None, False,
                      headline="A thing — PARTIALLY FIXED, one gap open.")
    assert it.claims_closure is False
    assert it.bucket == "open"


# ---------------------------------------------------------------------------
# The widened CLOSURE VOCABULARY — complaint 3
#
# The owner's words: "déjà vu every day dealing with the same stuff over and
# over". Finished items were queued alongside live work because the board did
# not recognise the words their authors had used.
#
# Two vocabularies exist on purpose (see the module's own comment): the build
# check's narrow one, and the renderer's wider one. These tests pin BOTH, and
# pin that widening the renderer did not widen the build check.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tail,expected", [
    # The words the owner reported, as they appear in his own backlog.
    ("A thing — SHIPPED 2026-09-04.", "finished"),
    ("A thing — REPLACED 2026-09-10.", "finished"),
    ("A thing — REDESIGNED 2026-09-11, owner call.", "finished"),
    ("A thing — LANDED 2026-09-04.", "finished"),
    ("A thing — SUPERSEDED by the rewrite.", "finished"),
    ("A thing — CLOSED 2026-09-04.", "finished"),
    ("A thing — DELIVERED.", "finished"),
    ("A thing — COMPLETE.", "finished"),
    ("A thing — COMPLETED 2026-09-04.", "finished"),
    # Still recognised from before the widening.
    ("A thing — FIXED 2026-09-10.", "finished"),
    ("A thing — DONE 2026-09-04.", "finished"),
    ("A thing — item WITHDRAWN 2026-09-03.", "finished"),
    # Work done, review owed — its own answer, never collapsed into "done".
    ("A thing — FIXED, pending review.", "review_owed"),
    ("A thing — SHIPPED, awaiting review.", "review_owed"),
    ("A thing — REPLACED, pending sign-off.", "review_owed"),
    # Work still outstanding — stays in the running order.
    ("A thing — MOSTLY FIXED, one real judgment call left.", "part_done"),
    ("A thing — PARTIALLY FIXED, one gap open.", "part_done"),
    ("A thing — PARTIALLY CLOSED, re-measured.", "part_done"),
    # No claim at all.
    ("A thing — TOO STRICT. IN FLIGHT.", ""),
    ("A thing — DEFECT. Observed, not theorised.", ""),
])
def test_the_widened_vocabulary_reads_the_three_states_apart(tail, expected):
    it = sb.QueueItem(1, "t", "", "", None, False, headline=tail)
    assert it.closure_claim == expected


@pytest.mark.parametrize("headline", [
    # The three real false positives this logic was built to survive. Every
    # one of them is a line from the live backlog.
    "Order-fill detection was a fixed-interval REST poll from 1992 — DEFECT.",
    "The sizing path still owes the same amendment — deliberately NOT done yet.",
    "An acceptance test is broken on main — STILL BROKEN, this file's own "
    "FIXED claim was wrong.",
    # And the same shapes built out of the NEWLY recognised words, which is
    # where a widened list would break first.
    "A fixed-interval poll — NOT YET SHIPPED.",
    "The stream rewrite — STILL OPEN, the REPLACED claim was premature.",
    "The prompt rewrite — TO BE REDESIGNED once item 18 lands.",
    "The ranking change — WILL BE SHIPPED after the re-measure.",
    "The sizing amendment — INCOMPLETE.",
])
def test_a_negated_or_future_status_is_not_a_closure_claim(headline):
    """A marker that cries wolf gets ignored, which costs more than not having
    the marker. Every one of the new words is a past participle, and a past
    participle in a plan reads identically to one in a result unless the tense
    in front of it is read too."""
    it = sb.QueueItem(1, "t", "", "", None, False, headline=headline)
    assert it.closure_claim == ""
    assert it.claims_closure is False
    assert it.bucket in ("open", "paused")


@pytest.mark.parametrize("headline", [
    # The word appears in the item's BODY half, describing something else. Only
    # the status half of a headline — after the last em dash — is a claim.
    "Slots burned re-proposing names that never get shipped — DEFECT.",
    "The budget guard we shipped in August is the wrong shape — TOO STRICT.",
    "A poll that was replaced everywhere else is still here — DEFECT.",
    "The scorecard was redesigned upstream and we never took it — NO RECORD.",
])
def test_an_incidental_use_of_a_new_word_is_not_a_closure_claim(headline):
    it = sb.QueueItem(1, "t", "", "", None, False, headline=headline)
    assert it.closure_claim == ""
    assert it.bucket == "open"


def test_a_word_that_merely_contains_a_closure_word_is_not_one():
    """Substring matching is what makes a growing vocabulary dangerous:
    INCOMPLETE contains COMPLETE, UNRESOLVED contains RESOLVED, and MERGE
    ORDER nearly contains MERGED. Matching is on word boundaries."""
    for tail in ("A thing — UNRESOLVED.",
                 "A thing — merge ORDER matters, see the incident history.",
                 "A thing — UNDONE by the next change."):
        it = sb.QueueItem(1, "t", "", "", None, False, headline=tail)
        assert it.closure_claim == "", tail


def test_widening_the_renderer_did_not_widen_the_build_failing_check(tmp_path):
    """The one hard constraint on complaint 3. The build check and the
    renderer read separate vocabularies precisely so the page can be made
    honest without turning live backlog lines into a red CI run — which only
    a backlog edit could then resolve."""
    assert "SHIPPED" in sb._RENDER_CLOSURE_WORDS
    assert "SHIPPED" not in sb._CLOSURE_WORDS
    assert "REPLACED" not in sb._CLOSURE_WORDS
    assert "REDESIGNED" not in sb._CLOSURE_WORDS
    # Every build-check word is still a rendering word: the vocabularies
    # diverge in one direction only.
    assert set(sb._CLOSURE_WORDS) <= set(sb._RENDER_CLOSURE_WORDS)

    # A newly-recognised word does NOT fail the build...
    p = tmp_path / "WORK.md"
    p.write_text("## THE FUNNEL QUEUE\n\n**4. A thing — SHIPPED 2026-09-04.**\n")
    assert sb.find_closed_items_not_marked_done(p) == []
    # ...while the page still shows it as finished rather than as live work.
    items = sb._parse_numbered_items(p.read_text().split(sb._QUEUE_HEADING, 1)[1])
    assert items[0].bucket == "finished_unmarked"


def test_the_real_backlog_no_longer_queues_finished_work_as_live():
    """The live outcome, pinned. These are the exact items the owner named."""
    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    items, problem = sb.load_funnel_queue(work_md)
    assert problem is None
    by_rank = {i.rank: i for i in items}

    # SHIPPED / REPLACED / REDESIGNED — finished, and no longer in the queue.
    for rank in (14, 36, 42, 43):
        assert by_rank[rank].bucket == "finished_unmarked", rank
    # "FIXED, pending review" — finished, review still owed, its own section.
    for rank in (33, 34):
        assert by_rank[rank].bucket == "review_owed", rank
    # RESOLVED 2026-09-11 — genuinely closed, struck through, no longer live.
    assert by_rank[2].bucket == "resolved"
    # Genuinely partial work stays where he can see it.
    for rank in (18, 32):
        assert by_rank[rank].bucket == "open", rank
        assert by_rank[rank].part_done is True, rank
    # And the negated lines stay open, as they always did.
    for rank in (28, 30):
        assert by_rank[rank].bucket == "open", rank


def test_a_mostly_finished_item_is_labelled_rather_than_hidden():
    """Moving partly-finished work out of the running order would hide live
    work, which is worse than the problem being fixed. It is labelled."""
    it = sb.QueueItem(32, "The risk envelope", "", "", None, False,
                      headline="The risk envelope — MOSTLY FIXED, one real "
                               "judgment call left.")
    assert it.bucket == "open"
    out = sb._render_open_queue([it], None)
    assert "partly done" in out
    assert "still outstanding" in out


def test_review_owed_is_neither_live_work_nor_signed_off():
    it = sb.QueueItem(33, "The two risk checks", "", "", None, False,
                      headline="The two risk checks — FIXED, pending review.")
    assert it.bucket == "review_owed"
    out = sb._render_review_owed([it])
    assert "the work is done" in out
    assert "a review is still owed" in out
    # Never struck through: a strike-through reads as signed off.
    assert "ol-done" not in out


def test_empty_finished_and_review_sections_say_so_rather_than_render_blank():
    assert "ticked off as finished" in sb._render_finished_unmarked([])
    assert "waiting on a review" in sb._render_review_owed([])


# ---------------------------------------------------------------------------
# IDENTIFIERS — complaint 1
#
# His words: he could see titles, but to ask about an item he had to recite a
# whole title rather than name a short handle that points back at the source
# of truth. The backlog runs more than one numbered sequence, so the handle
# has to disambiguate as well as identify.
# ---------------------------------------------------------------------------

def test_two_numbered_sequences_do_not_share_an_identifier():
    """The funnel queue counts 1..43 and the PM TEST GATE counts 1..8,
    independently. A bare number names two different things, so each sequence
    carries its own prefix and the prefix is part of what he quotes."""
    queue_item = sb.QueueItem(4, "A queue thing", "", "", None, False,
                              source="backlog")
    gate_item = sb.QueueItem(4, "A gate thing", "", "", None, False,
                             source="pm-gate")
    assert queue_item.ref == "item 4"
    assert gate_item.ref == "gate item 4"
    assert queue_item.ref != gate_item.ref


def test_the_real_backlog_gives_every_item_a_unique_quotable_identifier():
    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    queue, qp = sb.load_funnel_queue(work_md)
    gate, gp = sb.load_pm_gate(work_md)
    assert qp is None and gp is None
    refs = [i.ref for i in queue] + [i.ref for i in gate]
    assert len(refs) == len(set(refs)), "two items share one identifier"
    assert all(i.source == "backlog" for i in queue)
    assert all(i.source == "pm-gate" for i in gate)


def test_every_rendered_item_carries_its_identifier():
    """Every list he reads: the running order, the parked list, the finished
    list, the finished-but-untidy list and the review list."""
    it = sb.QueueItem(32, "A thing", "DEFECT", "", None, False,
                      headline="A thing — DEFECT.")
    assert "item 32" in sb._render_open_queue([it], None)
    assert "item 32" in sb._render_one_liners([it], "nothing")
    assert "item 32" in sb._render_one_liners([it], "nothing", struck=True)

    fin = sb.QueueItem(42, "A finished thing", "", "", None, False,
                       headline="A finished thing — REPLACED 2026-09-10.")
    assert "item 42" in sb._render_finished_unmarked([fin])
    rev = sb.QueueItem(33, "A reviewed thing", "", "", None, False,
                       headline="A reviewed thing — FIXED, pending review.")
    assert "item 33" in sb._render_review_owed([rev])


def test_a_decision_carries_a_quotable_identifier_too():
    """A pending decision has no number in the backlog — its shape is
    `- [ ] DECIDE BY <date> — question` — so the date is its only stable
    handle, and it is spelled out in full rather than abbreviated."""
    d = sb.PendingDecision(dt.date(2026, 9, 16), "A question", 5)
    assert d.ref == "decision due 2026-09-16"
    assert "decision due 2026-09-16" in sb._render_decisions([d])


def test_a_phase_carries_its_stage_identifier():
    p = _phase_with([sb.RuleResult("file_exists", sb.PASS, "")], title="A stage")
    assert "stage " + p.id in sb._row(p)


def test_only_references_actually_present_in_the_source_are_shown():
    """Nothing is looked up, derived or guessed. If the backlog names no PR,
    the page shows no PR — inventing one would be the staleness this board
    exists to prevent, wearing a tracking number."""
    assert sb.extract_refs("A plain item with no references at all.") == ()
    assert "PR #252" in sb.extract_refs("core cause MERGED 2026-09-04 (PR #252)")
    assert "incident history" in sb.extract_refs(
        "Detail: `docs/INCIDENT_HISTORY.md`, 2026-09-11.")
    assert "branch feat/replace-budget-reservation" in sb.extract_refs(
        "SHIPPED on `feat/replace-budget-reservation`.")


def test_a_documentation_path_is_not_announced_as_a_branch():
    """`docs/` is a real directory in this repo, so a naive branch pattern
    reports `docs/INCIDENT_HISTORY.md` to him as a branch name."""
    refs = sb.extract_refs("See `docs/INCIDENT_HISTORY.md` and `docs/OUTCOME.md`.")
    assert not any("branch" in r for r in refs)
    assert refs == ("incident history",)


def test_a_long_list_of_prs_is_counted_not_recited():
    """One real item names eleven PRs. Eleven chips is a list, not a
    reference."""
    refs = sb.extract_refs("Eleven PRs open at once (#249, #250, #251, #252, "
                           "#253, #254, #255, #256, #257, #261, #262).")
    assert refs == ("11 pull requests named in the backlog",)


def test_the_identifier_is_readable_but_does_not_dominate_the_card():
    """It has to be quotable on a phone without becoming the loudest thing on
    the card. Carried by a small monospace tag, not by heading type."""
    template = (Path(__file__).resolve().parents[1]
                / "scripts" / "status_board_template.html").read_text()
    assert ".ref{" in template
    ref_rule = template.split(".ref{", 1)[1].split("}", 1)[0]
    assert "IBM Plex Mono" in ref_rule
    assert "user-select:all" in ref_rule   # tap-and-copy on a phone
    # And it is not set at heading weight/size.
    assert "font-size:11.5px" in ref_rule


# ---------------------------------------------------------------------------
# Buckets: one item, one section
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headline,expected", [
    ("A thing — DEFERRED, not investigated further.", "paused"),
    ("A thing — MOOT, deleted with item 14.", "paused"),
    ("A thing — TOO STRICT. IN FLIGHT.", "open"),
])
def test_paused_items_are_separated_from_live_work(headline, expected):
    it = sb.QueueItem(1, "A thing", "", "", None, False, headline=headline)
    assert it.bucket == expected


def test_a_struck_through_item_is_resolved_not_open():
    body = "\n**~~12. A thing — FIXED 2026-09-03.~~**\n"
    items = sb._parse_numbered_items(body)
    assert items[0].done is True
    assert items[0].bucket == "resolved"


# ---------------------------------------------------------------------------
# RIGHT NOW — exactly one thing
# ---------------------------------------------------------------------------

def _decision(days_left, question="A question", prose=None):
    return sb.PendingDecision(dt.date.today() + dt.timedelta(days=days_left),
                              question, days_left, prose or sb.Prose())


def test_right_now_shows_exactly_one_thing():
    rotten = _phase_with([sb.RuleResult("file_exists", sb.FAIL, "")],
                         recorded="DONE AND LIVE", title="Rotten")
    out = sb._render_right_now(
        [rotten], [_decision(-3), _decision(2)],
        [sb.QueueItem(1, "Top item", "", "", None, False)])
    assert out.count('class="rn"') == 1


def test_rot_outranks_a_decision_which_outranks_the_running_order():
    rotten = _phase_with([sb.RuleResult("file_exists", sb.FAIL, "")],
                         recorded="DONE AND LIVE", title="Rotten")
    top = sb.QueueItem(1, "Top item", "", "", None, False)
    d = _decision(-3, "Overdue question")

    both = sb._render_right_now([rotten], [d], [top])
    assert "can no longer prove it is still finished" in both
    assert "Overdue question" not in both

    no_rot = sb._render_right_now([], [d], [top])
    assert "Overdue question" in no_rot
    assert "Top item" not in no_rot

    queue_only = sb._render_right_now([], [], [top])
    assert "Top item" in queue_only


def test_a_decision_far_in_the_future_does_not_outrank_the_running_order():
    top = sb.QueueItem(1, "Top item", "", "", None, False)
    out = sb._render_right_now([], [_decision(60, "Distant question")], [top])
    assert "Top item" in out
    assert "Distant question" not in out


def test_nothing_to_do_says_so_rather_than_inventing_urgency():
    out = sb._render_right_now([], [], [])
    assert "Nothing needs you" in out


# ---------------------------------------------------------------------------
# A decision carries its own explanation and recommendation
# ---------------------------------------------------------------------------

def test_a_decision_reads_its_plain_language_block_from_board_notes(tmp_path):
    """The decision's own line in docs/WORK.md carries only the question now
    — its prose comes from docs/BOARD_NOTES.md, keyed by the decision's due
    date (`PendingDecision.ref`), because a decision has no number of its
    own to key on."""
    p = tmp_path / "WORK.md"
    p.write_text(
        "- [ ] DECIDE BY 2099-01-01 — Which model runs the decision seat?\n"
    )
    notes = _notes(tmp_path, (
        "## decision due 2099-01-01\n\n"
        "**Plain language —** Which AI does the desk's final trade call.\n"
        "**Example —** Same shortlist, two models: one buys three names,\n"
        "the other buys one.\n"
        "**Recommendation —** Re-measure first, then decide.\n"
    ))
    got = sb.load_pending_decisions(p, today=dt.date(2098, 1, 1), notes=notes)
    assert len(got) == 1
    assert got[0].prose.plain.startswith("Which AI does")
    assert "two models" in got[0].prose.example
    assert got[0].prose.recommendation == "Re-measure first, then decide."


def test_a_decisions_indented_body_no_longer_carries_prose(tmp_path):
    """The relocation's guarantee for decisions too: prose typed straight
    into the indented body under a `DECIDE BY` line must not reach the page
    without a matching heading in docs/BOARD_NOTES.md."""
    p = tmp_path / "WORK.md"
    p.write_text(
        "- [ ] DECIDE BY 2099-01-01 — Which model runs the decision seat?\n"
        "  **Plain language —** this text is typed into the wrong file now.\n"
    )
    got = sb.load_pending_decisions(p, today=dt.date(2098, 1, 1))
    assert len(got) == 1
    assert got[0].prose.plain == ""
    assert not got[0].prose.has_any


def test_a_wrapped_question_is_not_truncated_to_a_fragment(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text(
        "- [ ] DECIDE BY 2099-01-01 — What should the freshness bar be,\n"
        "  given the real lag on the economic data?\n"
    )
    got = sb.load_pending_decisions(p, today=dt.date(2098, 1, 1))
    assert got[0].question.endswith("economic data?")


def test_a_decision_with_no_recommendation_says_so(tmp_path):
    p = tmp_path / "WORK.md"
    p.write_text("- [ ] DECIDE BY 2099-01-01 — A bare question?\n")
    got = sb.load_pending_decisions(p, today=dt.date(2098, 1, 1))
    out = sb._render_decisions(got)
    assert "A bare question?" in out
    assert "nothing here to agree or disagree with" in out


# ---------------------------------------------------------------------------
# It must never 500 his phone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "",
    "\x00\x01\x02 not markdown at all",
    "## THE FUNNEL QUEUE\n" + ("**1. " * 400) + "\n",
    "## THE FUNNEL QUEUE\n\n**notanumber. A thing — DEFECT.**\n",
    "- [ ] DECIDE BY 9999-99-99 — an impossible date\n",
])
def test_a_malformed_backlog_still_renders_a_page(tmp_path, content):
    """A board that fails to load is a board he stops opening. Whatever the
    backlog looks like, a page comes out and it does not pretend."""
    p = tmp_path / "WORK.md"
    p.write_text(content)
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": True, "circuit": "clear", "spend_today": 0.1,
             "sessions_today": 1, "box_sha": "abc", "main_sha": "abc"}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template, work_md=p)
    assert "{{" not in out
    assert "QAMC Desk Board" in out


def test_a_missing_backlog_file_does_not_break_the_page(tmp_path):
    phases = [_phase([sb.RuleResult("file_exists", sb.PASS, "note")])]
    state = {"in_sync": None, "circuit": None, "spend_today": None,
             "sessions_today": None, "box_sha": None, "main_sha": None}
    template = Path(__file__).resolve().parents[1] / "scripts" / "status_board_template.html"
    out = sb.render(phases, state, template, work_md=tmp_path / "gone.md")
    assert "{{" not in out
    assert "could not be read" in out or "missing" in out


# ---------------------------------------------------------------------------
# Accessibility and the rebuild trigger
# ---------------------------------------------------------------------------

def test_no_status_on_the_page_depends_on_colour_alone():
    """The owner is red/green colour blind. Every verdict has to survive all
    colour being stripped out, so each one must be a WORD in the markup."""
    for _cls, label in sb.VERDICT_PILL.values():
        assert label and label.strip() == label
        assert not label.lower() in ("red", "green", "amber")
    # And each label is real words, not a colour name or a bare symbol.
    labels = [lbl for _c, lbl in sb.VERDICT_PILL.values()]
    assert all(any(ch.isalpha() for ch in lbl) for lbl in labels)


def test_the_rebuild_trigger_watches_the_backlog():
    """The board's core defect before this change: the file it is made of was
    not watched, so editing the backlog did not update the owner's page."""
    unit = (Path(__file__).resolve().parents[1] / "scripts" / "systemd"
            / "quant-agent-status-board.path").read_text()
    assert "docs/WORK.md" in unit
    assert "PathChanged=/home/qamc/quant-agent/docs/WORK.md" in unit


def test_the_rebuild_trigger_also_watches_the_board_notes_file():
    """The prose the page renders now lives in docs/BOARD_NOTES.md, not
    docs/WORK.md. An edit to it changes what the board says exactly as much
    as an edit to the backlog does, so it must fire the same rebuild — the
    same defect the WORK.md watch above exists to prevent, on the other
    half of the page's source material."""
    unit = (Path(__file__).resolve().parents[1] / "scripts" / "systemd"
            / "quant-agent-status-board.path").read_text()
    assert "PathChanged=/home/qamc/quant-agent/docs/BOARD_NOTES.md" in unit


def test_the_board_service_does_not_point_at_the_retired_timer():
    """A .timer unit used to drive this and was replaced by the .path unit.
    Install instructions naming the timer would have an operator enable a unit
    that no longer exists."""
    svc = (Path(__file__).resolve().parents[1] / "scripts" / "systemd"
           / "quant-agent-status-board.service").read_text()
    enable_lines = [l for l in svc.splitlines()
                    if "systemctl" in l and "enable" in l]
    assert enable_lines
    assert all("status-board.timer" not in l for l in enable_lines)


@pytest.mark.parametrize("headline,expected", [
    # Real lines from the live backlog that a plain word search got wrong.
    # "a fixed-interval poll" DESCRIBES a mechanism; only the status half of a
    # headline is a claim, which is why this one is still False despite the
    # word "fixed" appearing in it.
    ("Order-fill detection was a fixed-interval REST poll — DEFECT.", False),
    ("The sizing path still owes an amendment — deliberately NOT done yet.",
     False),
    ("An acceptance test is broken on main — STILL BROKEN, this file's own "
     "FIXED claim was wrong.", False),
    # And the ones that genuinely do claim to be finished. "REPLACED" moved
    # from False to True deliberately: the owner reported it as a real
    # closure word his board was drawing as live work. See the widened
    # vocabulary tests above.
    ("Order-fill detection was a fixed-interval REST poll — REPLACED "
     "2026-09-10.", True),
    ("A protected-position rule — DONE 2026-09-04.", True),
    ("A broken ticker could fail silently forever — FIXED 2026-09-10.", True),
])
def test_a_description_is_not_read_as_a_closure_claim(headline, expected):
    """A marker that cries wolf gets ignored, which is worse than no marker.
    Only the STATUS half of a headline — after the last em dash — is a claim
    about where the item stands, and a negated status is not a claim at all."""
    it = sb.QueueItem(1, "t", "", "", None, False, headline=headline)
    assert it.claims_closure is expected


def test_the_real_backlog_flags_only_genuine_self_contradictions():
    """Pins the live outcome so a widened word list cannot quietly reintroduce
    false positives on the owner's own page. Reads the RENDERING vocabulary,
    which is the one the page is drawn from."""
    work_md = Path(__file__).resolve().parents[1] / "docs" / "WORK.md"
    items, problem = sb.load_funnel_queue(work_md)
    assert problem is None
    flagged = [i for i in items if i.bucket == "finished_unmarked"]
    assert flagged, "the live backlog has finished-but-unticked items"
    for i in flagged:
        tail = i.status_tail
        assert sb._closure_hit(tail, sb._RENDER_CLOSURE_WORDS), i.rank
        assert not any(w in tail for w in sb._CLOSURE_NEGATIONS), i.rank
        assert not sb._closure_hit(tail, sb._RENDER_PART_DONE_WORDS), i.rank


# ---------------------------------------------------------------------------
# The page's own copy must obey the page's own rules.
#
# The jargon detector ran only over prose loaded from docs/BOARD_NOTES.md.
# Every reader-facing string HARDCODED IN THIS SCRIPT was exempt from it —
# so the one card written by hand was the one card nothing checked. On
# 2026-09-11 that card told the owner three finished things had "stopped
# being true" and to "treat it as live breakage", printed internal stage
# identifiers, and carried no worked example. All three break rules this
# module's own docstring commits to, and none of it was catchable.
#
# These tests close that: the rules are enforced mechanically instead of
# depending on a session remembering to read the docstring.
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Card markup reduced to the words the owner actually reads. The jargon
    detector must not see tag names or class attributes — those are markup,
    not copy, and flagging them would make this check meaningless."""
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", html))


def _right_now_card_html():
    rotten = _phase_with(
        [sb.RuleResult("setting_equals", sb.FAIL, "the single-name ceiling",
                       "risk.max_position_pct = 100 (expected 20)")],
        recorded="DONE AND LIVE", title="Risk-based sizing")
    return sb._render_right_now([rotten], [], [])


def test_the_failed_proof_card_carries_a_worked_example():
    """The docstring promises every item gets "a concrete real-world
    example". The hand-written rot card silently did not."""
    assert "For example" in _right_now_card_html()


def test_the_failed_proof_card_names_the_real_numbers():
    """A card that says a proof failed without saying WHAT failed is a
    riddle. It must render the rule's own finding."""
    html = _right_now_card_html()
    assert "20" in html and "100" in html


def test_the_failed_proof_card_never_prints_an_internal_identifier():
    """No file paths, no function names, no code tokens — the whole premise
    of this page. The old card printed "(stage phase_2)"."""
    markers = sb.summary_engineering_markers(_strip_tags(_right_now_card_html()))
    assert markers == [], f"owner-facing card leaks engineer markers: {markers}"


def test_the_failed_proof_card_does_not_assert_breakage():
    """A failing evidence rule means the rule and the system DISAGREE. A
    deliberate settings change fails one identically to real breakage, so
    the card must not tell the owner his own decisions are breakage."""
    html = _strip_tags(_right_now_card_html()).lower()
    assert "live breakage" not in html
    assert "stopped being true" not in html


def test_a_humanised_identifier_keeps_a_date_readable():
    """2026_08_28 becoming "2026 08 28" reads as three unrelated numbers."""
    assert "2026-08-28" in sb._humanise_identifier(
        "test_the_estimator_reproduces_the_2026_08_28_block")


def test_no_board_note_is_orphaned_in_the_real_repository():
    """Every prose entry in the REAL docs/BOARD_NOTES.md must match a real
    item in the REAL docs/WORK.md.

    The keying tests above prove the mechanism. This proves the live files
    actually agree. Prose is keyed by item NUMBER, so renumbering an item in
    docs/WORK.md silently orphans the explanation written for it: the board
    cannot tell the difference between "this item was never explained" and
    "its explanation is sitting right there under the old number", and it
    honestly renders the owner's page as unexplained either way.

    That is a rule no session should have to remember. This is the check.
    """
    work = sb.REPO_ROOT / "docs" / "WORK.md"
    notes = sb.load_board_notes(sb.REPO_ROOT / "docs" / "BOARD_NOTES.md")
    queue, _ = sb.load_funnel_queue(work, notes)
    gate, _ = sb.load_pm_gate(work, notes)
    decisions = sb.load_pending_decisions(work, notes=notes)
    real = {x.ref for x in (*queue, *gate, *decisions)}

    orphans = sorted(k for k in notes if k not in real
                     and not k.lower().startswith("item n"))
    assert orphans == [], (
        "docs/BOARD_NOTES.md explains items that no longer exist under those "
        f"keys in docs/WORK.md: {orphans}. Either the item was renumbered "
        "(update the key in the same commit) or it was archived (remove its "
        "prose). Leaving it strands the explanation and the owner's board "
        "renders that item as never explained."
    )


def test_every_rendered_entry_carries_a_reference_handle():
    """The owner refers to an entry by a short handle ("item 44") instead of
    quoting a title that can run to a paragraph. An entry rendered without
    one cannot be talked about except by reading it out, which is exactly
    the friction the handle exists to remove.

    This asserts the handle is PRESENT on every entry. Whether it is legible
    is a styling question the template answers; whether it exists at all is
    this test's job, because an entry silently losing its handle would look
    fine on the page and only surface as the owner being unable to name it.
    """
    work = sb.REPO_ROOT / "docs" / "WORK.md"
    notes = sb.load_board_notes(sb.REPO_ROOT / "docs" / "BOARD_NOTES.md")
    queue, _ = sb.load_funnel_queue(work, notes)
    gate, _ = sb.load_pm_gate(work, notes)
    decisions = sb.load_pending_decisions(work, notes=notes)

    entries = [*queue, *gate, *decisions]
    assert entries, "no entries parsed — the fixture, not the rule, is wrong"
    missing = [getattr(e, "title", None) or getattr(e, "question", "?")
               for e in entries if not getattr(e, "ref", "").strip()]
    assert missing == [], (
        f"{len(missing)} board entries would render with no reference handle, "
        f"so the owner could not name them: {missing[:3]}"
    )
