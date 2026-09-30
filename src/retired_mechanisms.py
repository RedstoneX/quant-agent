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
#:
#: It is matched against the SOURCE line as well as the extracted prose.
#: That second half was added 2026-09-20 and it fixed a real defect, not a
#: nicety: `_prose_lines` hands `scan` the CONTENT of a string literal, so
#: a marker written as a trailing `#` comment on the same source line could
#: never reach it. A dict entry like
#: `"daily_loss_halted": "...",  # retired-ok` was therefore un-markable,
#: and five such markers already sat inert in `src/config.py` looking as
#: though they worked. Only whole-line comments could be marked, which is
#: not what this constant's own documentation said.
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
        source: list[str] | None = None
        for entry in entries:
            if rel in entry.allowed_in:
                continue
            if prose is None:
                prose = _prose_lines(path)
                source = path.read_text().splitlines()
            for lineno, line in prose:
                # The marker counts whether it is inside the extracted prose
                # or on the source line the prose came from — see
                # OPT_OUT_MARKER. Without the second read, a marker on a
                # string-literal line is invisible here.
                if OPT_OUT_MARKER in line:
                    continue
                if source is not None and 1 <= lineno <= len(source) \
                        and OPT_OUT_MARKER in source[lineno - 1]:
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


# ===========================================================================
# THE OTHER HALF: the TRIGGER, not the scan.
# ===========================================================================
#
# Everything above runs AFTER somebody has recorded a retirement. Its own
# docstring names the limit plainly: "a retirement nobody records. This is
# the real limit, and it is a convention." A convention is not enforcement,
# and the desk's meta-rule is that everything relying on remembering slips.
#
# `described:` closes that. It is the inverse index: a small list of LIVE
# code symbols that prompt text currently DESCRIBES, each paired with the
# exact places that description lives. Delete or rename one of those symbols
# and the build goes red at the deletion site, naming every sentence that is
# now a lie and telling you the one way out — move it to `retired:` with the
# phrases that described it, which is precisely the entry the scan above
# needs and nobody was remembering to write.
#
# WHY IT IS NOT A THIRD MECHANISM. There are exactly three jobs here and
# they do not overlap:
#
#   * `retired:` + `scan()` — the mechanism is GONE; find surviving prose by
#     the WORDS it used. Runs after the fact.
#   * `described:` + `described_gaps()` — this section. The mechanism is
#     ALIVE and described; fail the moment it stops existing. It is the
#     trigger that makes the first one fire, and it holds no phrases and
#     no digests of its own.
#   * `src/prompt_bindings.py` (board item 107) — the mechanism is alive and
#     its BEHAVIOUR changed while still existing; a digest on each side
#     forces the prose to be re-read. That is a different failure and it is
#     built there, not here. Check both before building anything new.
#
# WHY IT LIVES IN THIS FILE. It is the deletion site's own trigger, it reads
# the same registry, and splitting it out would have produced the extra grep
# module that board item 99(d) and item 107 both warn against.
#
# WHAT IT DELIBERATELY DOES NOT DO. It does not scan for symbols nobody
# registered. That was measured on 2026-09-26 and rejected: 430 snake_case
# tokens appear in `config/prompts/*.md` and 151 of them resolve to no
# Python definition at all, because they are enum literals and JSON field
# names the seats EMIT (`greed_top_chasing`, `thesis_invalid_if`,
# `fundamentals_mispricing`). A gate over those is 151 false positives on
# day one, and a check that cries wolf gets switched off — which costs more
# than not having it. The list is explicit for the same reason `retired:`
# is: maintenance is asked once, at the moment somebody has the facts open.
#
# PYTHON-ASSEMBLED STRINGS ARE THE POINT, NOT AN AFTERTHOUGHT. Both
# confirmed drift instances on this desk lived in strings Python builds at
# run time, not in a prompt file. `described_in` therefore takes any file,
# and the first three entries shipped point at `src/agents/*.py` and at no
# markdown at all.


@dataclass(frozen=True)
class DescribedSymbol:
    file: str
    symbol: str


@dataclass(frozen=True)
class DescribedIn:
    file: str
    contains: tuple[str, ...]


@dataclass(frozen=True)
class Described:
    name: str
    why: str
    symbols: tuple[DescribedSymbol, ...]
    described_in: tuple[DescribedIn, ...]


#: A `contains` needle shorter than this cannot reliably select one
#: sentence, and a needle that matches by accident is the noise that gets a
#: check disabled.
MIN_NEEDLE = 12


def _resolve_symbol(tree: ast.AST, dotted: str) -> ast.AST | None:
    """The def/class named by `dotted` (`name` or `Class.method`)."""
    node: ast.AST | None = tree
    for part in dotted.split("."):
        found: ast.AST | None = None
        for child in ast.iter_child_nodes(node):  # type: ignore[arg-type]
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ) and child.name == part:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def load_described(path: Path | str = REGISTRY_PATH) -> list[Described]:
    """The `described:` section. An absent section is legal and empty."""
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"retired-mechanism registry missing: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("described")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise RegistryError(f"{path}: `described:` must be a list")

    retired_symbols = {s for e in load_registry(path) for s in e.symbols}
    out: list[Described] = []
    seen: set[str] = set()
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            raise RegistryError(f"{path}: described entry #{i} is not a mapping")
        for required in ("name", "why", "symbols", "described_in"):
            if required not in item:
                raise RegistryError(
                    f"{path}: described entry #{i} "
                    f"({item.get('name', '?')}) has no `{required}`",
                )
        name = str(item["name"])
        if name in seen:
            raise RegistryError(f"{path}: two described entries named {name!r}")
        seen.add(name)

        symbols: list[DescribedSymbol] = []
        for s in item["symbols"] or ():
            if not isinstance(s, dict) or "file" not in s or "symbol" not in s:
                raise RegistryError(
                    f"{path}: `{name}` has a symbol without `file`/`symbol`",
                )
            dotted = str(s["symbol"])
            if dotted.split(".")[-1] in retired_symbols:
                raise RegistryError(
                    f"{path}: `{name}` says `{dotted}` is LIVE and described, "
                    f"while the `retired:` section says the same name is "
                    f"gone. One of the two is wrong and the build must not "
                    f"pass on a registry that contradicts itself.",
                )
            symbols.append(DescribedSymbol(str(s["file"]), dotted))
        if not symbols:
            raise RegistryError(
                f"{path}: `{name}` names no live symbol, so deleting "
                f"anything could never make it fire",
            )

        anchors: list[DescribedIn] = []
        for p in item["described_in"] or ():
            if not isinstance(p, dict) or "file" not in p:
                raise RegistryError(
                    f"{path}: `{name}` has a described_in without `file`",
                )
            needles = tuple(str(x) for x in (p.get("contains") or ()))
            if not needles:
                raise RegistryError(
                    f"{path}: `{name}` describes nothing in {p['file']}",
                )
            for needle in needles:
                if len(needle) < MIN_NEEDLE:
                    raise RegistryError(
                        f"{path}: needle {needle!r} in `{name}` is shorter "
                        f"than {MIN_NEEDLE} characters — too short to select "
                        f"one description, and a loose needle is noise",
                    )
            anchors.append(DescribedIn(str(p["file"]), needles))
        if not anchors:
            raise RegistryError(
                f"{path}: `{name}` records no place the description lives",
            )
        out.append(Described(
            name=name,
            why=str(item["why"]),
            symbols=tuple(symbols),
            described_in=tuple(anchors),
        ))
    return out


def described_gaps(
    root: Path | str = REPO_ROOT,
    registry_path: Path | str | None = None,
) -> list[str]:
    """Live mechanisms whose code, or whose description, has gone missing.

    Empty is a pass. Two failures, and they read differently on purpose:

      * THE SYMBOL IS GONE — a delete or a rename. Every sentence listed
        under it is now describing nothing. This is the deletion site.
      * THE DESCRIPTION IS GONE — the prose was rewritten or removed while
        the code stayed. The pointer has rotted; either re-point it or
        confirm the seat is meant to no longer be told.
    """
    root = Path(root)
    entries = load_described(
        registry_path or root / "config" / "retired_mechanisms.yaml",
    )
    problems: list[str] = []
    trees: dict[str, ast.AST | None] = {}
    for entry in entries:
        where = "; ".join(
            f"{a.file} ({', '.join(repr(n) for n in a.contains)})"
            for a in entry.described_in
        )
        why = " ".join(entry.why.split())[:260]
        for sym in entry.symbols:
            path = root / sym.file
            if not path.exists():
                problems.append(
                    f"`{entry.name}`: {sym.file} is gone, so "
                    f"`{sym.symbol}` cannot be there.\n"
                    f"    still described in: {where}\n"
                    f"    what the seats are told: {why}\n"
                    f"    if the mechanism was RETIRED, add it to the "
                    f"`retired:` section of config/retired_mechanisms.yaml "
                    f"with the phrases above, delete this entry, and fix "
                    f"every sentence the scan then reports.",
                )
                continue
            if sym.file not in trees:
                try:
                    trees[sym.file] = ast.parse(
                        path.read_text(), filename=str(path),
                    )
                except SyntaxError as exc:
                    raise RegistryError(f"cannot parse {sym.file}: {exc}") from exc
            tree = trees[sym.file]
            if tree is None or _resolve_symbol(tree, sym.symbol) is None:
                problems.append(
                    f"`{entry.name}`: {sym.file} no longer defines "
                    f"`{sym.symbol}` — it was deleted or renamed.\n"
                    f"    still described in: {where}\n"
                    f"    what the seats are told: {why}\n"
                    f"    A RENAME: re-point this entry, one line. A "
                    f"DELETION: move it to the `retired:` section with the "
                    f"phrases that described it, delete this entry, and fix "
                    f"every sentence the scan then reports. The desk PAYS a "
                    f"model to read some of these lines.",
                )
        for anchor in entry.described_in:
            apath = root / anchor.file
            if not apath.exists():
                problems.append(
                    f"`{entry.name}`: described_in points at {anchor.file}, "
                    f"which does not exist.",
                )
                continue
            text = apath.read_text()
            for needle in anchor.contains:
                if needle not in text:
                    problems.append(
                        f"`{entry.name}`: {anchor.file} no longer contains "
                        f"{needle!r}.\n"
                        f"    The description moved or was deleted while the "
                        f"code it describes is still live. Re-point this "
                        f"entry, or confirm the seat is deliberately no "
                        f"longer told: {why}",
                    )
    return problems
