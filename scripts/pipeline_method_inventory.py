"""The method inventory of the pipeline modules, read from the AST.

Step 0 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). The inventory is read
from the abstract syntax tree and never from `import`/`dir()`, so a move that
breaks an import still reports the truth about what each file CONTAINS.

`python -m scripts.pipeline_method_inventory` prints the current inventory as a
diff against the recorded one; `--write` rewrites the recorded file, which is
what a later step does when it deliberately moves a method.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY_PATH = REPO_ROOT / "tests" / "pipeline_method_inventory.json"

#: The modules the split touches. A later step ADDS its new module here in the
#: same change that moves the code, which is the point of the guard.
TRACKED_MODULES: tuple[str, ...] = (
    "src/pipeline.py",
    "src/pipeline_delever.py",
    "src/pipeline_exits.py",
    "src/pipeline_prompt_facts.py",
    "src/pipeline_protection.py",
    "src/pipeline_risk_gate.py",
    "src/pipeline_research_continuity.py",
    "src/pipeline_stages.py",
)

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_inventory(path: Path) -> dict[str, object]:
    """Every module-level function name and every method name per class, from the AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = sorted(n.name for n in tree.body if isinstance(n, _FUNC))
    classes: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = sorted(
                child.name for child in node.body if isinstance(child, _FUNC)
            )
    return {
        "module_functions": functions,
        "classes": dict(sorted(classes.items())),
    }


def current_inventory(root: Path | None = None) -> dict[str, dict[str, object]]:
    base = root or REPO_ROOT
    return {rel: module_inventory(base / rel) for rel in TRACKED_MODULES}


def recorded_inventory(path: Path | None = None) -> dict[str, dict[str, object]]:
    return json.loads((path or INVENTORY_PATH).read_text(encoding="utf-8"))["modules"]


def write_inventory(path: Path | None = None, root: Path | None = None) -> Path:
    target = path or INVENTORY_PATH
    payload = {
        "_why": (
            "Frozen method inventory of the pipeline modules, read from the AST "
            "(docs/PIPELINE_SPLIT_PLAN.md step 0, board item 210). A step that MOVES "
            "a method updates this file in the same change; an unexplained difference "
            "is a method that vanished or was duplicated."
        ),
        "_regenerate": "python -m scripts.pipeline_method_inventory --write",
        "modules": current_inventory(root),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the recorded inventory")
    args = parser.parse_args()
    if args.write:
        print(f"wrote {write_inventory()}")
        return 0
    current = current_inventory()
    try:
        recorded = recorded_inventory()
    except FileNotFoundError:
        print("no recorded inventory yet; run with --write")
        return 1
    drifted = False
    for rel, now in current.items():
        was = recorded.get(rel)
        if was != now:
            drifted = True
            print(f"{rel}: inventory differs from the recorded one")
    for rel in recorded:
        if rel not in current:
            drifted = True
            print(f"{rel}: recorded but no longer tracked")
    print("inventory matches" if not drifted else "inventory DRIFTED")
    return 1 if drifted else 0


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    raise SystemExit(main())
