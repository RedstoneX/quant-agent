"""A sandbox checkout writes only inside itself, measured, and the check can fail.

`scripts/check_sandbox_isolation.py` runs the desk's writers under an audit
hook with a throwaway home and working directory.  These tests prove it passes
on the real tree AND that it fails when a writer escapes, because a check that
cannot fail would pass with every writer pointing at production.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_script = REPO / "scripts" / "check_sandbox_isolation.py"
_spec = importlib.util.spec_from_file_location("check_sandbox_isolation", _script)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_the_real_tree_is_isolated_and_leaves_a_guarded_tree_untouched(tmp_path: Path) -> None:
    guarded = tmp_path / "production_stand_in"
    (guarded / "data").mkdir(parents=True)
    (guarded / "data" / "quant_agent.db").write_bytes(b"production rows")
    violations, data = check.run_check(REPO, sys.executable, [guarded])
    assert violations == []
    assert data["writes_inside_checkout"] > 0
    assert (guarded / "data" / "quant_agent.db").read_bytes() == b"production rows"


def test_derivation_reaches_the_root_entry_point_and_is_not_a_stored_list() -> None:
    modules = check.derive_modules(REPO)
    assert "main" in modules
    assert "src.data_paths" in modules
    assert "src.storage.db" in check.derive_writer_modules(REPO)


def test_a_writer_that_escapes_the_checkout_is_caught(tmp_path: Path, monkeypatch) -> None:
    escaped = tmp_path / "elsewhere"
    escaped.mkdir()
    real_copy = check.shutil.copytree

    def copy_with_escaping_writer(source, dest, *args, **kwargs):
        result = real_copy(source, dest, *args, **kwargs)
        if Path(source) == REPO / "src":
            (Path(dest) / "escaping_writer.py").write_text(
                f"from pathlib import Path\nPath({str(escaped)!r}, 'leak.txt').write_text('x')\n"
            )
        return result

    monkeypatch.setattr(check.shutil, "copytree", copy_with_escaping_writer)
    violations, _ = check.run_check(REPO, sys.executable, [])
    assert any("leak.txt" in v for v in violations)
    assert (escaped / "leak.txt").exists()
