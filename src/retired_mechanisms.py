"""Fail the build when prose still describes a mechanism the code deleted.

THE DEFECT. Deleting code does not delete the sentences about it. On
2026-09-14 the daily-loss breaker stopped liquidating the book (docs/WORK.md
item 32); `_midday_emergency_liquidate` went with it. Three days later four
places still described the deleted behaviour, and TWO of them were text a paid
model reads: `src/agents/position_reviewer.py` assembles the reviewer's user
message, and it told the seat the desk performs an "emergency sell-all on −3%
daily-loss breach" every time any safety net had fired that day. Both the
mechanism and the number were gone.

WHY A CHECK AT THE DELETION SITE, AND NOT THE OBVIOUS ALTERNATIVES. Four
designs were weighed against the case that actually happened:

  * RENDER THE NUMBERS. `src/agents/prompt_limits.py` already does this for
    two sheets and it is the right answer FOR NUMBERS — extended in the same
    change to the one hand-typed daily-loss figure it had missed. It cannot
    help here: nothing was wrong with a number. A mechanism described in
    words stopped existing.
  * EXTRACT ASSERTIONS FROM PROMPT PROSE AND CHECK THEM. Nothing can read
    "the desk performs an emergency sell" and know which function that is.
  * ANNOTATE EACH ASSERTION WITH A TIE TO WHAT IT DESCRIBES. Workable, and
    it would have caught this — but only if somebody had annotated that
    sentence years of prompt-writing ago. It asks for maintenance on every
    sentence forever, and the failure mode being closed is precisely that
    nobody remembers the prompts exist.
  * ANY NUMBER IN A PROMPT MUST MATCH A CONSTANT. Measured: 1,828 numeric
    tokens across `config/prompts/*.md`, mostly list numbering, dates and
    figures inside worked examples. On the order of a thousand annotations,
    and it catches neither confirmed case. Dead on arrival.

This one asks for maintenance ONCE, at the moment somebody has the facts in
front of them — the commit that deletes the mechanism. After that it is free.

WHAT IT CATCHES: any surviving description of a retired mechanism, in prompt
files, in the Python that assembles prompts, and in docstrings and comments,
by the words used rather than by the symbol name. Run against the tree as it
stood on 2026-09-16 it finds all four stale sites.

WHAT IT DOES NOT CATCH, stated plainly so this module is never cited as if it
did:

  * a mechanism whose behaviour CHANGED without being deleted. Nothing is
    retired, so nothing is listed, so nothing is scanned.
  * drift in a description nobody retired — a threshold that moved, a
    sequence of steps reordered, a guarantee weakened.
  * a phrase not on the list. Somebody writes the list.
  * a retirement nobody records. This is the real limit, and it is a
    convention. It is a convention at the one point in the work where the
    facts are known and the diff is open, which is the best available
    trade; it is not enforcement.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "config" / "retired_mechanisms.yaml"

#: A line carrying this marker is a deliberate historical mention — a
#: tombstone comment at a deletion site, or a prompt telling a seat that a
#: trigger does NOT match. Per LINE, not per file: allowing a whole file is
#: how `src/pipeline.py` would have gone on hiding the `run_intra_check`
#: docstring, one of the four sites this check exists to find.
OPT_OUT_MARKER = "retired-ok"

#: Where prose about the desk lives. Prompt markdown, the agent modules that
#: assemble prompts in Python (the surface the 2026-09-14 case actually hid
#: in), and the pipeline/risk modules whose docstrings describe the same
#: machinery.
SCAN_GLOBS: tuple[str, ...] = (
    "config/prompts/*.md",
    "src/agents/*.py",
    "src/*.py",
    "src/risk/*.py",
    "src/execution/*.py",
    "src/evolution/*.py",
)


class RegistryError(RuntimeError):
    """The registry is malformed. Distinct from a finding: this one means the
    check could not run, which must never read as a pass."""


@dataclass(frozen=True)
class Retired:
    name: str
    retired: str
    why: str
    symbols: tuple[str, ...]
    phrases: tuple[str, ...]
    allowed_in: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    mechanism: str
    matched: str
    kind: str          # "phrase" | "symbol"
    text: str
    why: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.path}:{self.line}  describes a mechanism retired on "
            f"{self.mechanism}\n"
            f"    matched {self.kind}: {self.matched!r}\n"
            f"    line: {self.text.strip()[:150]}\n"
            f"    truth: {' '.join(self.why.split())[:220]}"
        )


def load_registry(path: Path | str = REGISTRY_PATH) -> list[Retired]:
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"retired-mechanism registry missing: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("retired")
    if not isinstance(entries, list):
        raise RegistryError(f"{path}: top-level `retired:` must be a list")
    out: list[Retired] = []
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            raise RegistryError(f"{path}: entry #{i} is not a mapping")
        for required in ("name", "retired", "why", "allowed_in"):
            if required not in item:
                raise RegistryError(
                    f"{path}: entry #{i} ({item.get('name', '?')}) has no "
                    f"`{required}`",
                )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item["retired"])):
            raise RegistryError(
                f"{path}: entry `{item['name']}` has retired="
                f"{item['retired']!r}; use YYYY-MM-DD, taken from git",
            )
        symbols = tuple(str(s) for s in (item.get("symbols") or ()))
        phrases = tuple(str(s).lower() for s in (item.get("phrases") or ()))
        if not symbols and not phrases:
            raise RegistryError(
                f"{path}: entry `{item['name']}` lists neither a symbol nor a "
                f"phrase, so it checks nothing",
            )
        for phrase in phrases:
            if len(phrase) < 8:
                raise RegistryError(
                    f"{path}: phrase {phrase!r} in `{item['name']}` is too "
                    f"short to be specific — a broad phrase turns this check "
                    f"into noise and noise gets it switched off",
                )
        out.append(Retired(
            name=str(item["name"]),
            retired=str(item["retired"]),
            why=str(item["why"]),
            symbols=symbols,
            phrases=phrases,
            allowed_in=tuple(str(s) for s in item["allowed_in"]),
        ))
    return out


def _scan_targets(root: Path) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in SCAN_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                seen.setdefault(path, None)
    return list(seen)


def _prose_lines(path: Path) -> list[tuple[int, str]]:
    """Lines of this file that are PROSE — what a reader or a model sees.

    For markdown, every line. For Python, only comments, docstrings and
    string literals: a phrase inside an identifier or a keyword list is not
    a description of anything, and flagging it would be the noise that gets
    a check disabled.
    """
    text = path.read_text()
    if path.suffix != ".py":
        return list(enumerate(text.splitlines(), start=1))

    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append((lineno, stripped))

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - a broken file fails elsewhere
        raise RegistryError(f"cannot parse {path}: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lineno = getattr(node, "lineno", 1)
            for offset, piece in enumerate(node.value.splitlines() or [""]):
                out.append((lineno + offset, piece))
    return out


def scan(
    root: Path | str = REPO_ROOT,
    registry_path: Path | str | None = None,
) -> list[Finding]:
    """Every surviving description of a retired mechanism. Empty is a pass."""
    root = Path(root)
    entries = load_registry(registry_path or root / "config" / "retired_mechanisms.yaml")
    findings: list[Finding] = []
    for path in _scan_targets(root):
        rel = path.relative_to(root).as_posix()
        prose: list[tuple[int, str]] | None = None
        for entry in entries:
            if rel in entry.allowed_in:
                continue
            if prose is None:
                prose = _prose_lines(path)
            for lineno, line in prose:
                if OPT_OUT_MARKER in line:
                    continue
                low = line.lower()
                for phrase in entry.phrases:
                    if phrase in low:
                        findings.append(Finding(
                            rel, lineno, f"{entry.retired} ({entry.name})",
                            phrase, "phrase", line, entry.why,
                        ))
                for symbol in entry.symbols:
                    if symbol in line:
                        findings.append(Finding(
                            rel, lineno, f"{entry.retired} ({entry.name})",
                            symbol, "symbol", line, entry.why,
                        ))
    return findings


def resurrected_symbols(
    root: Path | str = REPO_ROOT,
    registry_path: Path | str | None = None,
) -> list[str]:
    """Retired symbols that are DEFINED again somewhere in `src/`.

    A retirement that quietly comes back leaves every description of it
    correct and the registry wrong, which is the one way this check can lie.
    """
    root = Path(root)
    entries = load_registry(registry_path or root / "config" / "retired_mechanisms.yaml")
    wanted = {s: e.name for e in entries for s in e.symbols}
    if not wanted:
        return []
    back: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ) and node.name in wanted:
                back.append(
                    f"{path.relative_to(root).as_posix()}:{node.lineno} "
                    f"defines `{node.name}`, retired as part of "
                    f"'{wanted[node.name]}'",
                )
    return back


def format_findings(findings: list[Finding]) -> str:  # pragma: no cover
    return "\n\n".join(str(f) for f in findings)
