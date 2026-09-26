"""Force the prose to be re-read whenever the behaviour it describes changes.

THE HOLE THIS FILLS, AND WHY IT IS NOT THE ONE ALREADY CLOSED.
`src/retired_mechanisms.py` catches a mechanism that was DELETED while
sentences about it survived. Its own docstring names what it cannot catch,
first item: "a mechanism whose behaviour CHANGED without being deleted.
Nothing is retired, so nothing is listed, so nothing is scanned." That is
this module. Do not build a third grep — check here first, as board item
107 instructs.

Drift by change is the commoner half. A threshold moves, a branch is added,
a guarantee is weakened, the order of two steps swaps. Nothing is removed,
so no deletion-site convention fires, and the paid seats go on reading a
description that was true last month. `config/prompts/tech_analyst.md`
spells out the constructor's stop rule to four figures ("2.5", "reachable
range 2.14-3.00", "within a quarter of an ATR"); every one of those is a
sentence about code in another file, and nothing has ever tied them
together.

WHAT A BINDING IS. A named pairing of (a) one or more CODE ANCHORS — a
function, by file and name — with (b) the PROMPT LINES that describe what
that code does, selected by an anchor substring rather than by line number
so that editing the file above them does not churn. Each side is pinned by
a digest. The check fails when the two sides disagree about whether they
changed.

WHY A DIGEST AND NOT AN ASSERTION ABOUT MEANING. The alternative designs
were weighed on 2026-09-17 and three were rejected for this class (see
`src/retired_mechanisms.py` and docs/INCIDENT_HISTORY.md); the one that
survived asks for maintenance once, at the moment the facts are open. A
digest keeps that property exactly: nobody annotates a sentence in advance,
nobody writes a rule a machine has to understand. The registry is a list of
"these two things are about the same thing", and the build makes you look
at the second when you touch the first.

THE CODE DIGEST IGNORES COSMETICS ON PURPOSE. It is taken over the parsed
syntax tree with docstrings dropped and without source positions, so
reformatting, renaming a local, moving the function, or rewriting its
docstring does not fire. What fires is a change to what the function
computes — which is precisely when somebody should re-read the prose.

IT IS STILL A CONVENTION AT THE EDGES, SAID PLAINLY. It only covers pairs
somebody registered. It cannot tell a prose fix from a prose regression; it
can only insist a human looked. A change confined to a function nobody
bound is invisible to it. Those limits are the same ones the deletion-site
check accepts, for the same reason: an enforcement that asks for annotation
on every sentence forever is the design that was rejected.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "config" / "prompt_bindings.yaml"


class BindingError(RuntimeError):
    """The registry is malformed or points at something that is not there.

    Distinct from a finding. A registry that cannot be read must never be
    reported as a clean run.
    """


@dataclass(frozen=True)
class CodeAnchor:
    file: str
    symbol: str


@dataclass(frozen=True)
class ProseAnchor:
    file: str
    contains: tuple[str, ...]


@dataclass(frozen=True)
class Binding:
    name: str
    why: str
    code: tuple[CodeAnchor, ...]
    prose: tuple[ProseAnchor, ...]
    code_digest: str
    prose_digest: str


def _strip_docstring(node: ast.AST) -> ast.AST:
    """Drop a leading string statement from every body we digest.

    A docstring is prose about the function, not behaviour. Digesting it
    would make this check fire on exactly the edit it is trying to
    encourage — somebody improving the explanation.
    """
    for child in ast.walk(node):
        body = getattr(child, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            del body[0]
    return node


def _find_symbol(tree: ast.AST, symbol: str) -> ast.AST | None:
    """The function/class named `symbol`, allowing `Class.method`."""
    parts = symbol.split(".")
    node: ast.AST | None = tree
    for part in parts:
        found = None
        for child in ast.iter_child_nodes(node):  # type: ignore[arg-type]
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ) and child.name == part:
                found = child
                break
        if found is None:
            # One level of nesting inside a class body is the common shape;
            # fall back to a full walk so `Class.method` still resolves when
            # the class is itself nested.
            for child in ast.walk(node):  # type: ignore[arg-type]
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ) and child.name == part:
                    found = child
                    break
        if found is None:
            return None
        node = found
    return node


def code_digest(root: Path, anchors: tuple[CodeAnchor, ...]) -> str:
    """A digest of WHAT the bound functions compute, not how they are typed."""
    parts: list[str] = []
    for anchor in anchors:
        path = root / anchor.file
        if not path.exists():
            raise BindingError(
                f"code anchor {anchor.file}:{anchor.symbol} — no such file. "
                f"If the mechanism was deleted rather than changed, retire it "
                f"in config/retired_mechanisms.yaml and drop this binding.",
            )
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            raise BindingError(f"cannot parse {anchor.file}: {exc}") from exc
        node = _find_symbol(tree, anchor.symbol)
        if node is None:
            raise BindingError(
                f"code anchor {anchor.file}:{anchor.symbol} — no such "
                f"function or class. A rename is a change: update the "
                f"binding and re-read the prose it is paired with.",
            )
        parts.append(ast.dump(_strip_docstring(node), include_attributes=False))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def prose_digest(root: Path, anchors: tuple[ProseAnchor, ...]) -> str:
    """A digest of the prompt LINES that describe the bound behaviour.

    Selected by substring, so inserting a paragraph above them does not
    fire and editing one of them does.
    """
    parts: list[str] = []
    for anchor in anchors:
        path = root / anchor.file
        if not path.exists():
            raise BindingError(f"prose anchor {anchor.file} — no such file")
        lines = path.read_text().splitlines()
        for needle in anchor.contains:
            hits = [ln.strip() for ln in lines if needle in ln]
            if not hits:
                raise BindingError(
                    f"prose anchor {anchor.file}: nothing contains "
                    f"{needle!r} any more. Either the description moved — "
                    f"re-point the binding — or it was deleted, which is "
                    f"itself a thing to confirm was deliberate.",
                )
            parts.append(f"{anchor.file}::{needle}::" + "\n".join(hits))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def load_registry(path: Path | str = REGISTRY_PATH) -> list[Binding]:
    path = Path(path)
    if not path.exists():
        raise BindingError(f"behaviour-change registry missing: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("bindings")
    if not isinstance(entries, list):
        raise BindingError(f"{path}: top-level `bindings:` must be a list")
    if not entries:
        raise BindingError(
            f"{path}: the registry is empty, which would make this check "
            f"pass while checking nothing",
        )
    out: list[Binding] = []
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            raise BindingError(f"{path}: entry #{i} is not a mapping")
        for required in ("name", "why", "code", "prose", "code_digest", "prose_digest"):
            if required not in item:
                raise BindingError(
                    f"{path}: entry #{i} ({item.get('name', '?')}) has no "
                    f"`{required}`",
                )
        code = tuple(
            CodeAnchor(str(c["file"]), str(c["symbol"])) for c in item["code"]
        )
        prose = tuple(
            ProseAnchor(
                str(p["file"]),
                tuple(str(x) for x in (p.get("contains") or ())),
            )
            for p in item["prose"]
        )
        if not code or not prose:
            raise BindingError(
                f"{path}: entry `{item['name']}` binds nothing on one side, "
                f"so it can never disagree with itself",
            )
        for p in prose:
            if not p.contains:
                raise BindingError(
                    f"{path}: prose anchor {p.file} in `{item['name']}` "
                    f"selects no lines",
                )
            for needle in p.contains:
                if len(needle) < 8:
                    raise BindingError(
                        f"{path}: anchor {needle!r} in `{item['name']}` is "
                        f"too short to select one description reliably",
                    )
        out.append(Binding(
            name=str(item["name"]),
            why=str(item["why"]),
            code=code,
            prose=prose,
            code_digest=str(item["code_digest"]),
            prose_digest=str(item["prose_digest"]),
        ))
    return out


def check(
    root: Path | str = REPO_ROOT,
    registry_path: Path | str | None = None,
) -> list[str]:
    """Every binding whose two sides no longer agree. Empty is a pass."""
    root = Path(root)
    bindings = load_registry(registry_path or root / "config" / "prompt_bindings.yaml")
    problems: list[str] = []
    for binding in bindings:
        actual_code = code_digest(root, binding.code)
        actual_prose = prose_digest(root, binding.prose)
        code_moved = actual_code != binding.code_digest
        prose_moved = actual_prose != binding.prose_digest
        if not code_moved and not prose_moved:
            continue
        where_code = ", ".join(f"{c.file}:{c.symbol}" for c in binding.code)
        where_prose = ", ".join(p.file for p in binding.prose)
        if code_moved and not prose_moved:
            headline = (
                "the BEHAVIOUR changed and the prose describing it did not"
            )
        elif prose_moved and not code_moved:
            headline = (
                "the prose changed and the behaviour did not — confirm the "
                "new wording is still true, then re-pin"
            )
        else:
            headline = (
                "both sides changed — confirm they changed to agree, then "
                "re-pin"
            )
        problems.append(
            f"binding `{binding.name}`: {headline}.\n"
            f"    code:  {where_code}\n"
            f"    prose: {where_prose}\n"
            f"    what they describe: {' '.join(binding.why.split())[:240]}\n"
            f"    re-pin with: python -m scripts.repin_prompt_bindings\n"
            f"    code_digest: {binding.code_digest} -> {actual_code}\n"
            f"    prose_digest: {binding.prose_digest} -> {actual_prose}",
        )
    return problems


def current_digests(
    root: Path | str = REPO_ROOT,
    registry_path: Path | str | None = None,
) -> dict[str, tuple[str, str]]:
    """`{name: (code_digest, prose_digest)}` as the tree stands right now."""
    root = Path(root)
    bindings = load_registry(registry_path or root / "config" / "prompt_bindings.yaml")
    return {
        b.name: (code_digest(root, b.code), prose_digest(root, b.prose))
        for b in bindings
    }
