"""The shrink-only baseline merge driver, exercised through REAL git merges.

Each test builds a throwaway repository, registers
`scripts/git_merge_driver_baselines.sh` as `baselinemerge` exactly as
`scripts/install_git_merge_drivers.sh` does, commits a base, changes the same
baseline on two branches, and runs `git merge`. Nothing here calls a helper.
"""
from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "git_merge_driver_baselines.sh"
SIZE = "tests/file_size_baseline.json"
# Only a baseline still STORED needs a merge driver. The file-size,
# silent-swallow and __new__-pipeline ratchets and the import-cycle guard now
# compute their reference from origin/main at check time and store nothing, so
# none has a file for a merge driver to register
# (docs/GUARDS_WITHOUT_STORED_STATE.md).
BASELINES = [
    "tests/import_layers.json",
]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stdout}{r.stderr}")
    return r


def make_repo(tmp_path: Path, rel: str, base: dict, files: tuple[str, ...] = ()) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "merge.baselinemerge.name", "t")
    git(repo, "config", "merge.baselinemerge.driver", f"{DRIVER} %O %A %B %P")
    (repo / ".gitattributes").write_text(f"{rel} merge=baselinemerge\n")
    write(repo, rel, base)
    for f in files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("x = 1\n")
    git(repo, "add", ".gitattributes", rel, *files)
    git(repo, "commit", "-qm", "base")
    return repo


def write(repo: Path, rel: str, data: dict) -> None:
    (repo / rel).write_text(json.dumps(data, indent=1) + "\n")


def commit_branch(repo: Path, branch: str, rel: str, data: dict, delete: tuple[str, ...] = ()) -> None:
    git(repo, "checkout", "-q", "-B", branch, "main") if branch != "main" else git(repo, "checkout", "-q", "main")
    write(repo, rel, data)
    for d in delete:
        git(repo, "rm", "-q", d)
    git(repo, "add", rel)
    git(repo, "commit", "-qm", branch)


def test_three_way_merge_takes_minimum_of_both_sides(tmp_path):
    base = {"a.py": 900, "b.py": 900, "c.py": 900}
    repo = make_repo(tmp_path, SIZE, base, ("a.py", "b.py", "c.py", "new1.py", "new2.py"))
    commit_branch(repo, "left", SIZE, {"a.py": 850, "b.py": 900, "c.py": 880, "new1.py": 820})
    commit_branch(repo, "main", SIZE, {"a.py": 870, "b.py": 700, "c.py": 900, "new2.py": 810})
    r = git(repo, "merge", "left", "-m", "m", check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((repo / SIZE).read_text())
    assert got == {"a.py": 850, "b.py": 700, "c.py": 880, "new1.py": 820, "new2.py": 810}


def test_merge_that_loosens_a_baseline_is_refused(tmp_path):
    base = {"a.py": 900, "b.py": 900}
    repo = make_repo(tmp_path, SIZE, base, ("a.py", "b.py"))
    commit_branch(repo, "left", SIZE, {"a.py": 950, "b.py": 900})
    commit_branch(repo, "main", SIZE, {"a.py": 960, "b.py": 800})
    r = git(repo, "merge", "left", "-m", "m", check=False)
    assert r.returncode != 0, "a loosening merge must not resolve"
    text = (repo / SIZE).read_text()
    assert "<<<<<<<" in text, "refusal must leave conflict markers, not a silent copy"
    assert "LARGER" in (r.stdout + r.stderr)
    assert SIZE in git(repo, "diff", "--name-only", "--diff-filter=U").stdout


def test_key_for_file_deleted_on_one_side_is_dropped(tmp_path):
    base = {"gone.py": 900, "stay.py": 900}
    repo = make_repo(tmp_path, SIZE, base, ("gone.py", "stay.py"))
    commit_branch(repo, "left", SIZE, {"gone.py": 900, "stay.py": 850}, delete=("gone.py",))
    commit_branch(repo, "main", SIZE, {"gone.py": 880, "stay.py": 900})
    r = git(repo, "merge", "left", "-m", "m", check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (repo / "gone.py").exists()
    assert json.loads((repo / SIZE).read_text()) == {"stay.py": 850}


def test_frozen_set_baseline_keeps_removals_and_refuses_growth(tmp_path):
    rel = "tests/silent_swallow_baseline.json"
    base = {"_comment": "c", "keys": ["a.py::f#1", "b.py::g#1", "c.py::h#1"]}
    repo = make_repo(tmp_path, rel, base, ("a.py", "b.py", "c.py"))
    commit_branch(repo, "left", rel, {**base, "keys": ["b.py::g#1", "c.py::h#1"]})
    commit_branch(repo, "main", rel, {**base, "keys": ["a.py::f#1", "b.py::g#1"]})
    assert git(repo, "merge", "left", "-m", "m", check=False).returncode == 0
    assert json.loads((repo / rel).read_text())["keys"] == ["b.py::g#1"]


def test_frozen_set_baseline_refuses_a_grown_entry(tmp_path):
    rel = "tests/pipeline_new_baseline.json"
    base = {"_comment": "c", "files": ["tests/a.py"]}
    repo = make_repo(tmp_path, rel, base, ("tests/a.py", "tests/z.py"))
    commit_branch(repo, "left", rel, {**base, "files": ["tests/a.py", "tests/z.py"]})
    commit_branch(repo, "main", rel, {**base, "files": []})
    assert git(repo, "merge", "left", "-m", "m", check=False).returncode != 0
    assert "<<<<<<<" in (repo / rel).read_text()


def test_import_layers_exact_importers_shrink_and_refuse_widening(tmp_path):
    rel = "tests/import_layers.json"
    rule = {"name": "seam", "target_prefix": "src.x", "exact_importers": ["m.a", "m.b", "m.c"], "why": "w"}
    base = {"_comment": "c", "rules": [rule]}
    repo = make_repo(tmp_path, rel, base)
    commit_branch(repo, "left", rel, {**base, "rules": [{**rule, "exact_importers": ["m.b", "m.c"]}]})
    commit_branch(repo, "main", rel, {**base, "rules": [{**rule, "exact_importers": ["m.a", "m.b"]}]})
    assert git(repo, "merge", "left", "-m", "m", check=False).returncode == 0
    assert json.loads((repo / rel).read_text())["rules"][0]["exact_importers"] == ["m.b"]


def test_every_ratchet_baseline_is_registered_in_gitattributes():
    attrs = (REPO_ROOT / ".gitattributes").read_text().splitlines()
    for rel in BASELINES:
        assert f"{rel} merge=baselinemerge" in attrs, rel


def test_warns_loudly_when_driver_not_registered_in_this_clone():
    r = subprocess.run(["git", "config", "--get", "merge.baselinemerge.driver"],
                       cwd=REPO_ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        warnings.warn(
            "merge driver `baselinemerge` is NOT registered in this clone: baseline JSON conflicts "
            "will fall back to git's plain merge. Run scripts/install_git_merge_drivers.sh once "
            "(README.md '### Install').", UserWarning, stacklevel=1)
