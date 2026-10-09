"""Which `(table, column)` pairs real code actually WRITES, read from source.

WHY THIS EXISTS. A settlement recording in state `built` must name the fields
its evidence lands in, and that claim has to be falsifiable. The first version
of the check was keyed on a PROXY: it split every string constant in THREE
HARDCODED storage files into words and accepted the bare column name if the
word appeared anywhere. Three ways to be wrong followed from that one mistake,
and all three are the same mistake -- matching a WORD instead of resolving an
IDENTITY:

  * a column passed because its name occurred somewhere in those files, even
    though nothing wrote the table in question;
  * a genuine write living in any OTHER file could not satisfy the check at
    all, however real the recording;
  * the table was never compared, so a column name shared by two tables
    passed on whichever table happened to use it.

This module answers the identity question instead: it parses every module
under `src/` and extracts the table and column list of each `INSERT INTO` and
`UPDATE ... SET` statement it can resolve statically. A column counts for a
table only when a real write names it for THAT table. `_ensure_column`
migrations, `CREATE TABLE` bodies, docstrings and comments are not writes and
never appear here, which is the defect this whole check was built for.

THE PROXY THIS MODULE ACCEPTS, AND WHICH WAY IT ERRS
----------------------------------------------------
A SQL string assembled at runtime cannot always be resolved from source. Two
shapes are resolved deliberately, because they are how real stores here spell
their writes: `f"... ({', '.join(NAME)})"` where `NAME` is a module-level
tuple or list of plain strings, and `f"UPDATE t SET {col} = ?"` inside a
`for col in (<literal strings>)` loop. EVERY OTHER dynamic shape (a column list
built from a function argument, a name assembled by concatenation, SQL read
from a variable defined in another module) is NOT resolved, and its columns
are therefore absent from the index.

That is a STRICT proxy and it is chosen, not inherited: an unresolvable write
makes the check REFUSE a recording that may well be real. It never admits one
that is fake. A caller that cannot read the source at all gets an exception,
never an empty index -- an empty index would wave everything through, which is
the failure mode this module exists to end.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: `INSERT INTO <table> (<columns>)`, with the optional `OR REPLACE` /
#: `OR IGNORE` conflict clause SQLite allows between the two keywords.
_INSERT = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.I | re.S)
#: `UPDATE <table> SET <assignments>` up to the first `WHERE`.
_UPDATE = re.compile(r"UPDATE\s+([A-Za-z_]\w*)\s+SET\s+(.*?)(?:\bWHERE\b|$)", re.I | re.S)
#: The upsert tail of an INSERT; its columns belong to the INSERT's table.
_DO_UPDATE = re.compile(
    r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\s+(.*?)(?:\bWHERE\b|$)",
    re.I | re.S,
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _columns(segment: str) -> set[str]:
    """Column names out of a bare list or a list of `col = <expr>` pairs."""
    names: set[str] = set()
    for part in segment.split(","):
        name = part.split("=")[0].strip().strip('"').strip("'").strip("`")
        if "." in name:
            name = name.rsplit(".", 1)[1]
        if _IDENT.match(name):
            names.add(name)
    return names


def _string_sequences(tree: ast.AST) -> dict[str, str]:
    """Module-level `NAME = (...)` tuples/lists of plain strings, pre-joined.

    This is the one dynamic shape resolved on purpose; see the module
    docstring for why, and for what is deliberately left unresolved.
    """
    found: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(value, (ast.Tuple, ast.List)):
            continue
        items = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if items and len(items) == len(value.elts):
            found[target.id] = ", ".join(items)
    return found


def _loop_strings(tree: ast.AST) -> dict[str, list[str]]:
    """Loop targets bound over a literal sequence of strings.

    The excursion columns are written by one `UPDATE trades SET {column} = ?`
    inside `for column, value in (("max_adverse_excursion", ...), ...)`. The
    write is real and the column names are right there in the source, so the
    loop variable is resolved to the literal names it takes.
    """
    bound: dict[str, set[str]] = {}

    def _record(target: ast.AST, element: ast.AST) -> None:
        if isinstance(target, ast.Name) and isinstance(element, ast.Constant):
            if isinstance(element.value, str):
                bound.setdefault(target.id, set()).add(element.value)
        elif isinstance(target, ast.Tuple) and isinstance(element, ast.Tuple):
            for sub_target, sub_element in zip(target.elts, element.elts):
                _record(sub_target, sub_element)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.comprehension)):
            continue
        source = node.iter
        if not isinstance(source, (ast.Tuple, ast.List, ast.Set)):
            continue
        for element in source.elts:
            _record(node.target, element)
    return {name: sorted(values) for name, values in bound.items()}


#: Marks an f-string hole whose value is a resolved loop variable, so the
#: statement can be expanded once per literal that variable takes.
_HOLE = "\x00"


def _expand(texts: list[str], loop_values: dict[str, list[str]]) -> list[str]:
    """One copy of each statement per literal its loop variables take."""
    for name, values in loop_values.items():
        marker = _HOLE + name
        if not any(marker in text for text in texts):
            continue
        texts = [text.replace(marker, value) for text in texts for value in values]
    return [text for text in texts if _HOLE not in text]


def _joined_name(node: ast.AST) -> str | None:
    """The `NAME` of a `<sep>.join(NAME)` call, or None for any other shape."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr != "join" or len(node.args) != 1:
        return None
    arg = node.args[0]
    return arg.id if isinstance(arg, ast.Name) else None


def _sql_strings(
    tree: ast.AST,
    sequences: dict[str, str],
    loop_values: dict[str, list[str]] | None = None,
) -> list[str]:
    """Every statically resolvable SQL-bearing string in the module.

    An f-string contributes its literal parts; a `', '.join(NAME)` hole is
    filled when `NAME` is a known module-level string sequence, and otherwise
    left as a gap, so an unresolved column list yields no columns rather than
    wrong ones.
    """
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif isinstance(piece, ast.FormattedValue):
                    name = _joined_name(piece.value)
                    if name is not None:
                        parts.append(sequences.get(name, ""))
                    elif isinstance(piece.value, ast.Name) and (piece.value.id in (loop_values or {})):
                        parts.append(_HOLE + piece.value.id)
                    else:
                        parts.append("")
            texts.extend(_expand(["".join(parts)], loop_values or {}))
    return texts


def columns_written_by(source: str) -> frozenset[tuple[str, str]]:
    """`(table, column)` pairs written by one module's source text."""
    tree = ast.parse(source)
    sequences = _string_sequences(tree)
    pairs: set[tuple[str, str]] = set()
    for text in _sql_strings(tree, sequences, _loop_strings(tree)):
        upper = text.upper()
        if "INSERT" not in upper and "UPDATE" not in upper:
            continue
        inserts = _INSERT.findall(text)
        for table, listed in inserts:
            for column in _columns(listed):
                pairs.add((table.lower(), column))
            for tail in _DO_UPDATE.findall(text):
                for column in _columns(tail):
                    pairs.add((table.lower(), column))
        if inserts and _DO_UPDATE.search(text):
            # The `DO UPDATE SET` tail already matched `_UPDATE`; its columns
            # are credited above, to the INSERT's own table.
            continue
        for table, assignments in _UPDATE.findall(text):
            for column in _columns(assignments):
                pairs.add((table.lower(), column))
    return frozenset(pairs)


def written_columns(source: str | None = None, root: Path | None = None) -> frozenset[tuple[str, str]]:
    """Every `(table, column)` real code writes, computed from source, now.

    Nothing is stored and no list of storage files is written down anywhere:
    a guard that records WHERE the writers live goes stale in silence the
    first time one moves. Every module under `root` is parsed instead.

    Raises rather than returning an empty set when the source cannot be read
    or cannot be parsed. A gate that cannot see the code must refuse, not
    wave everything through.
    """
    if source is not None:
        return columns_written_by(source)
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    modules = sorted(base.rglob("*.py"))
    if not modules:
        raise FileNotFoundError(
            f"no Python sources under {base}; the storage-write index cannot "
            f"be computed and no settlement route can be checked against it"
        )
    pairs: set[tuple[str, str]] = set()
    for path in modules:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(f"cannot read {path} for the storage-write index") from exc
        try:
            pairs |= columns_written_by(text)
        except SyntaxError as exc:
            raise SyntaxError(f"cannot parse {path} for the storage-write index: {exc}") from exc
    if not pairs:
        raise ValueError(
            f"parsed {len(modules)} modules under {base} and found no write "
            f"statements at all; refusing rather than passing every route"
        )
    return frozenset(pairs)
