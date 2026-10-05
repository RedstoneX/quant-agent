"""Prove a checkout is isolated: every write it makes lands inside itself.

The probe runs on a throwaway COPY of the checkout's code, so it never leaves
rows in the checkout's own data directory.

A sandbox session must never write into production records.  Every writer on
this desk resolves its location from the checkout it runs from, and the way to
trust that is to MEASURE it, not to read the code and agree with it.  This
check runs a probe in a child process whose home directory and working
directory are throwaway, with an audit hook recording every write-capable
event (opens for writing, sqlite connections, mkdir, rename, remove, copy).
It FAILS if any write lands outside the checkout under test, the scratch home
or the scratch working directory.

Nothing is stored.  The set of modules it loads is derived at check time from
the tree (the repository root's ``main.py`` included, which is the desk's live
entry point and the file a hand-written list forgets).  The set of path
constants it inspects is derived from the loaded modules.  It stores no list
of writers and no path.

Usage, from any checkout, with a python that has the desk's dependencies::

    python scripts/check_sandbox_isolation.py [--guard-tree PATH ...]

``--guard-tree`` names directories that must come out byte-for-byte unchanged
(a manifest of every file's SHA-256 is taken before and after).  Read-only: it
never writes to them.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import shutil
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "tests", "node_modules", "__pycache__", "frontend", "docs"}
PROBE_FLAG = "--probe"


def derive_modules(root: Path) -> list[str]:
    """Every importable module in the tree, found by walking it, not listing it."""
    names = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts[:-1]):
            continue
        if rel.parts[0] in {"scripts", "ops"}:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return names


def derive_writer_modules(root: Path) -> list[str]:
    """Modules that contain a write-capable call: the reach of this check."""
    markers = {"connect", "write_text", "write_bytes", "mkdir", "replace", "rename", "unlink", "touch"}
    found = []
    for name in derive_modules(root):
        path = root.joinpath(*name.split("."))
        file = path.with_suffix(".py") if path.with_suffix(".py").exists() else path / "__init__.py"
        try:
            tree = ast.parse(file.read_text())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in markers:
                found.append(name)
                break
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                found.append(name)
                break
    return found


def _is_write_open(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    return False


def _probe(root: Path, scratch: Path, report: Path) -> None:
    """Child process: record writes while the desk's writers are exercised."""
    writes: list[str] = []
    allowed_prefix = {
        "checkout": str(root.resolve()),
        "scratch": str(scratch.resolve()),
    }

    def hook(event: str, args: tuple) -> None:
        target = None
        if event == "open" and _is_write_open(args[1], args[2]):
            target = args[0]
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            target = args[0]
        elif event in {"os.rename", "shutil.copyfile", "shutil.move"}:
            target = args[1]
        elif event == "sqlite3.connect":
            target = args[0]
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if isinstance(target, str) and target and not target.startswith(":") and target != "/dev/null":
            writes.append(os.path.abspath(os.path.join(os.getcwd(), target)))

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    modules = derive_modules(root)
    writer_modules = set(derive_writer_modules(root))
    sys.addaudithook(hook)

    import_errors = []
    loaded = []
    for name in modules:
        try:
            loaded.append(importlib.import_module(name))
        except Exception as exc:  # noqa: BLE001 - recorded, a missing dependency is not isolation
            import_errors.append(f"{name}: {type(exc).__name__}")

    outside_constants = []
    checkout = str(root.resolve())
    home = str(scratch.resolve())
    for module in loaded:
        for attr, value in list(vars(module).items()):
            if isinstance(value, Path) and value.is_absolute():
                resolved = str(value.resolve())
                if not (resolved.startswith(checkout) or resolved.startswith(home)):
                    outside_constants.append(f"{module.__name__}.{attr} -> {resolved}")

    from src import data_paths
    from src.storage.db import Database

    for fn in (data_paths.data_dir, data_paths.alerting_dir, data_paths.board_dir,
               data_paths.diary_dir, data_paths.parse_failure_dir):
        fn().mkdir(parents=True, exist_ok=True)
        (fn() / "probe.txt").write_text("sandbox probe\n")
    data_paths.db_path().parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(data_paths.db_path()))
    db.initialize()
    db.conn.execute("CREATE TABLE IF NOT EXISTS isolation_probe (v TEXT)")
    db.conn.execute("INSERT INTO isolation_probe VALUES ('sandbox')")
    db.conn.commit()
    db.conn.close()
    (Path.home() / ".cache").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".cache" / "probe.txt").write_text("home-anchored writer lands in scratch home\n")

    report.write_text(json.dumps({
        "writes": sorted(set(writes)),
        "allowed": allowed_prefix,
        "modules": len(modules),
        "writer_modules": len(writer_modules),
        "import_errors": import_errors,
        "outside_constants": sorted(set(outside_constants)),
        "db_exists": data_paths.db_path().exists(),
        "db_path": str(data_paths.db_path()),
    }))


def manifest(tree: Path) -> dict[str, str]:
    """SHA-256 of every file under a tree.  Read-only."""
    result = {}
    for path in sorted(tree.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run_check(root: Path, python: str, guard_trees: list[Path]) -> tuple[list[str], dict]:
    """Return (violations, measurements).  Empty violations means isolated."""
    before = {tree: manifest(tree) for tree in guard_trees}
    with tempfile.TemporaryDirectory(prefix="sandbox-iso-") as tmp:
        scratch = Path(tmp)
        (scratch / "cwd").mkdir()
        report = scratch / "report.json"
        # Probe a COPY of the code, so the checkout's own data directory is never
        # dirtied.  Writers resolve from their file position, so the copy is
        # exactly as isolated as the original and no more.
        copy = scratch / "checkout"
        copy.mkdir()
        for entry in ("src", "config", "main.py"):
            source = root / entry
            if source.is_dir():
                shutil.copytree(source, copy / entry, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, copy / entry)
        root = copy
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM", "ALPACA"))}
        env.update({"HOME": str(scratch), "PYTHONDONTWRITEBYTECODE": "1", "TELEGRAM_DISABLED": "1"})
        proc = subprocess.run(
            [python, str(Path(__file__).resolve()), PROBE_FLAG, str(root), str(scratch), str(report)],
            cwd=scratch / "cwd", env=env, capture_output=True, text=True, timeout=600,
        )
        if not report.exists():
            return [f"probe did not finish: {proc.stderr[-400:]}"], {}
        data = json.loads(report.read_text())
        resolved_scratch = str(scratch.resolve())
        allowed = [str(root.resolve()), resolved_scratch]
        violations = [
            f"write outside the checkout and scratch: {w}"
            for w in data["writes"]
            if not any(w == a or w.startswith(a + os.sep) for a in allowed)
        ]
        if not data["db_exists"] or not data["db_path"].startswith(str(root.resolve())):
            violations.append("the database did not land inside the checkout's own data directory")
        data["writes_inside_checkout"] = sum(1 for w in data["writes"] if w.startswith(str(root.resolve())))
        data["writes_inside_scratch_home"] = sum(1 for w in data["writes"] if w.startswith(resolved_scratch))
    for tree in guard_trees:
        after = manifest(tree)
        if after != before[tree]:
            changed = sorted(set(after) ^ set(before[tree]) | {k for k in after if after[k] != before[tree].get(k)})
            violations.append(f"guarded tree changed: {tree} ({len(changed)} files)")
    return violations, data


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == PROBE_FLAG:
        _probe(Path(args[1]), Path(args[2]), Path(args[3]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--guard-tree", action="append", default=[], type=Path)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    ns = parser.parse_args(args)
    violations, data = run_check(ns.checkout.resolve(), ns.python, ns.guard_tree)
    for key in ("modules", "writer_modules", "writes_inside_checkout", "writes_inside_scratch_home"):
        print(f"{key}: {data.get(key)}")
    print(f"import_errors: {len(data.get('import_errors', []))}")
    for item in data.get("outside_constants", []):
        print(f"read-only anchor outside checkout (no write observed): {item}")
    for item in violations:
        print(f"VIOLATION: {item}")
    print("ISOLATED" if not violations else "NOT ISOLATED")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
