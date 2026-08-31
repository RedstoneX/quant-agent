"""The status board must never report a wrong status confidently.

The board exists because every hand-maintained status document in this repo
went stale — five wrong claims inside two days. Its value therefore rests
entirely on one property: **it would rather say `unknown` than say something
false.** These tests pin that property, and they pin the specific bug the very
first live run exposed, where a malformed rule masqueraded as documentation rot.
"""

from __future__ import annotations

import importlib.util
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
                "{{RULES_UNKNOWN}}", "{{BOX_SHA}}", "{{MAIN_SHA}}"):
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
    """
    import yaml

    manifest = Path(__file__).resolve().parents[1] / "docs" / "phases.yaml"
    raw = yaml.safe_load(manifest.read_text())
    phases = raw["phases"] if isinstance(raw, dict) and "phases" in raw else raw
    hit = [e for e in phases if str(e.get("id")) == "open_defects"]
    assert hit, "no open_defects entry in the manifest"
    defects = hit[0].get("defects")
    assert isinstance(defects, list) and defects, (
        "open_defects carries no defects: list"
    )
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
        "or decided content has likely crept back in; cut or move it rather "
        "than raising this number"
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
