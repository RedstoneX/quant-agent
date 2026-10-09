"""Import-boundary contracts (import-linter, config in /.importlinter).

Only a fixed allow-list of gateway modules may import the broker SDK.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _lint_imports() -> str:
    found = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    return found or "lint-imports"


def _run(cwd: Path, config: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_lint_imports(), "--config", str(config)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_repo_import_contracts_hold():
    result = _run(REPO, REPO / ".importlinter")
    assert result.returncode == 0, result.stdout + result.stderr


def test_unlisted_module_importing_alpaca_is_rejected(tmp_path):
    pkg = tmp_path / "fakesrc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "rogue.py").write_text("import alpaca\n")
    config = tmp_path / ".importlinter"
    config.write_text(
        "[importlinter]\n"
        "root_packages =\n    fakesrc\n"
        "include_external_packages = True\n\n"
        "[importlinter:contract:gateway]\n"
        "name = gateway\n"
        "type = forbidden\n"
        "source_modules =\n    fakesrc\n"
        "forbidden_modules =\n    alpaca\n"
        "allow_indirect_imports = true\n"
    )
    result = _run(tmp_path, config)
    assert result.returncode != 0, result.stdout
    assert "fakesrc.rogue -> alpaca" in result.stdout
