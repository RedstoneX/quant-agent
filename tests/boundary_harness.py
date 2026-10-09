"""Boundary checker: clauses 1-5 of docs/ARCHITECTURE.md section 3 (target-architecture).

A boundary exists where a piece can be constructed and exercised on its own,
without building a TradingPipeline. Pure AST; imports nothing from src.
Moves no product code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PIPELINE_MODULE = "src.pipeline"
PIPELINE_CLASS = "TradingPipeline"


@dataclass
class Verdict:
    module: str
    failures: dict = field(default_factory=dict)  # clause number -> reason
    warnings: list = field(default_factory=list)  # reported, not failing
    skipped: list = field(default_factory=list)  # clauses not checkable

    @property
    def passed(self) -> bool:
        return not self.failures


def _parse(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _mod_path(module: str) -> Path:
    return ROOT / (module.replace(".", "/") + ".py")


def references_pipeline(tree: ast.AST, *, strings: bool = True) -> bool:
    """True if the tree names TradingPipeline (import, name, attribute, or a
    string such as a patch target) or imports the src.pipeline module."""
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module == PIPELINE_MODULE or any(a.name == PIPELINE_CLASS for a in n.names):
                return True
        elif isinstance(n, ast.Import):
            if any(a.name == PIPELINE_MODULE for a in n.names):
                return True
        elif isinstance(n, ast.Name) and n.id == PIPELINE_CLASS:
            return True
        elif isinstance(n, ast.Attribute) and n.attr == PIPELINE_CLASS:
            return True
        elif (
            strings
            and isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and (PIPELINE_CLASS in n.value or n.value.startswith(PIPELINE_MODULE + ".") or n.value == PIPELINE_MODULE)
        ):
            return True
    return False


def imports_module(tree: ast.AST, module: str) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == module:
            return True
        if isinstance(n, ast.Import) and any(a.name == module for a in n.names):
            return True
        if (
            isinstance(n, ast.ImportFrom)
            and n.module == module.rpartition(".")[0]
            and any(a.name == module.rpartition(".")[2] for a in n.names)
        ):
            return True
    return False


#: Tests that are not measured: the harness's own files and the composition
#: root. These are the ONLY files the metric skips; there is no list of
#: current offenders; those are pinned by path in
#: config/check_allowlists/struct_boundary_pipeline_files.txt.
_UNMEASURED = (
    "boundary_harness.py",
    "test_boundary_harness.py",
    # and the composition root,
    "pipeline_factory.py",
    "test_pipeline_factory.py",
    # and the ONE deliberate whole-system test. test_e2e_morning_session.py
    # exists precisely to build a real TradingPipeline and drive every
    # stage end to end; counting it would penalise the test this metric
    # most wants to exist. Every OTHER test that needs a whole pipeline
    # is what the ratchet is measuring.
    "test_e2e_morning_session.py",
)


def test_files_referencing_pipeline(tests_dir: Path = TESTS) -> set[str]:
    """The ratchet metric, as IDENTITIES: the repo-relative path of every test
    file that imports or names TradingPipeline in code (imports, names,
    attributes; prose strings excluded)."""
    found: set[str] = set()
    for p in sorted(tests_dir.rglob("*.py")):
        # Any tests/test_e2e_*.py is a deliberate whole-system test (policy widened on main;
        # the permitted count is NOT raised): counting it would penalise the tests this metric wants.
        if p.name in _UNMEASURED or (p.name.startswith("test_e2e_") and p.suffix == ".py"):
            continue
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        if references_pipeline(tree, strings=False):
            found.add(p.relative_to(tests_dir.parent).as_posix())
    return found


def count_test_files_importing_pipeline(tests_dir: Path = TESTS) -> int:
    """How many test files reference TradingPipeline; a report figure, never a baseline."""
    return len(test_files_referencing_pipeline(tests_dir))


def _self_reads_and_writes(cls: ast.ClassDef):
    init_assigned, reads = set(), set()
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    class_level = set()
    for n in cls.body:
        if isinstance(n, ast.Assign):
            class_level |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            class_level.add(n.target.id)
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                if isinstance(n.ctx, ast.Store):
                    if fn.name == "__init__":
                        init_assigned.add(n.attr)
                else:
                    reads.add(n.attr)
    return init_assigned, reads, methods | class_level


def _pipeline_params(tree: ast.AST):
    """(annotated, duck) parameter names: annotated = hint names TradingPipeline;
    duck = untyped parameter named `pipeline`/`pipe`."""
    annotated, duck = [], []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                ann = ast.unparse(arg.annotation) if arg.annotation else ""
                if PIPELINE_CLASS in ann:
                    annotated.append(f"{n.name}({arg.arg})")
                elif arg.arg in ("pipeline", "pipe") and arg.arg != "self":
                    duck.append(f"{n.name}({arg.arg})")
    return annotated, duck


def _upward_imports(tree, layer_of, module):
    mine = layer_of(module)
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src"):
            theirs = layer_of(n.module)
            if mine is not None and theirs is not None and theirs >= mine:
                bad.append(n.module)
    return bad


def check_boundary(module: str, *, layer_of=None, tests_dir: Path = TESTS) -> Verdict:
    """Apply clauses 1-5 to a module ('src.pipeline_sizing').

    Function-only modules (no classes) have no constructor and no self, so
    clauses 1-2 are vacuous for them; clause 5 still requires a test.
    layer_of: optional callable module -> int|None for clause 4 (the layer
    manifest is conversion step 1); without it clause 4 is reported skipped.
    """
    v = Verdict(module)
    path = _mod_path(module)
    tree = _parse(path)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]

    # Clauses 1 and 2, per class.
    for cls in classes:
        init_assigned, reads, own = _self_reads_and_writes(cls)
        if "__init__" not in {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}:
            v.failures.setdefault(1, []).append(f"{cls.name}: no __init__")
        foreign = sorted(reads - init_assigned - own)
        if foreign:
            v.failures.setdefault(2, []).append(
                f"{cls.name}: {len(foreign)} self attrs not set in __init__ nor defined on it, e.g. {foreign[:3]}"
            )

    # Clause 3.
    if references_pipeline(tree, strings=False):  # code only: docstrings may name it
        v.failures[3] = "references src.pipeline / TradingPipeline"
    annotated, duck = _pipeline_params(tree)
    if annotated:
        v.failures[3] = f"accepts TradingPipeline parameter: {annotated[:3]}"
    if duck:
        v.warnings.append(
            f"clause 3: {len(duck)} untyped parameter(s) named pipeline/pipe (duck-typed pipeline), e.g. {duck[:3]}"
        )

    # Clause 4.
    if layer_of is None:
        v.skipped.append(4)
    else:
        up = _upward_imports(tree, layer_of, module)
        if up:
            v.failures[4] = f"imports at/above own layer: {up}"

    # Clause 5: a test that imports the module and never names TradingPipeline.
    witnesses = []
    for p in sorted(tests_dir.rglob("test_*.py")):
        try:
            t = _parse(p)
        except SyntaxError:
            continue
        if imports_module(t, module) and not references_pipeline(t):
            witnesses.append(p.name)
    if not witnesses:
        v.failures[5] = "no pipeline-free test imports this module"
    return v
