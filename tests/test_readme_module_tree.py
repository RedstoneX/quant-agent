"""The README's repository-layout block may not name paths that do not exist.

WHY. `README.md` is edited by roughly a third of all changes, and the layout
block inside it names modules by hand. A hand-maintained list of what is on
disk rots silently every time a module is renamed or deleted in a change that
forgets the README -- and a reader then goes looking for a file that is not
there. The paths are now DERIVED against the tree by
`scripts/readme_tree.py`, so the rot cannot land.

This guard is one-directional on purpose, and the docstring of
`scripts/readme_tree.drift` says why: the block is a curated tour with a
hand-written description per module, not an inventory, so a module on disk
that the block does not mention is not a defect. A path the block DOES name
and that does not exist always is.
"""

from __future__ import annotations

import subprocess

from scripts.readme_tree import README, committed_tree, drift, extract_block, listed_paths


def test_every_path_the_readme_names_exists_in_the_tree() -> None:
    problems = drift()
    assert problems == [], "README.md layout block has drifted:\n" + "\n".join(problems)


def test_the_layout_block_is_found_and_is_not_empty() -> None:
    """A guard that silently finds nothing to check is not a guard."""
    open_i, close_i, body = extract_block(README.read_text(encoding="utf-8"))
    assert close_i > open_i + 10
    paths = listed_paths(body)
    assert len(paths) >= 40, f"only {len(paths)} paths parsed out of the layout block"
    assert "src" in paths and "config/settings.yaml" in paths


def test_the_guard_actually_fails_on_a_renamed_module() -> None:
    """Pin the guard's teeth: a path that does not exist must be reported."""
    text = README.read_text(encoding="utf-8")
    _, _, body = extract_block(text)
    tree = committed_tree(body)
    assert "src" in tree, "expected the block to enumerate src/"
    assert "pipeline.py" in tree["src"]
    hypothetical = text.replace("├── pipeline.py", "├── pipeline_renamed_away.py", 1)
    assert hypothetical != text
    import scripts.readme_tree as module

    try:
        module.README = _FakeReadme(hypothetical)
        problems = module.drift()
    finally:
        module.README = README
    assert any("pipeline_renamed_away.py" in line for line in problems), problems


class _FakeReadme:
    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text


def test_the_block_round_trips_without_losing_a_single_line() -> None:
    """Nothing in the committed block is rewritten by this machinery: prove
    the parser is read-only by reassembling the file from its own pieces."""
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    open_i, close_i, body = extract_block(text)
    rebuilt = "".join(lines[: open_i + 1]) + "\n".join(body) + "\n" + "".join(lines[close_i:])
    assert rebuilt == text


def test_logs_directory_is_ignored_so_the_guard_stays_honest() -> None:
    """`logs/` is documented and never committed; if it stops being ignored
    the guard would start failing for a reason that is not drift."""
    result = subprocess.run(["git", "check-ignore", "logs/"], capture_output=True, text=True, cwd=README.parent)
    assert result.returncode == 0, "logs/ must stay gitignored"
