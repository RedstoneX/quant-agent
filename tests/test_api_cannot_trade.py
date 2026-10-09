"""Structural guard: the owner dashboard (`src/api/`) cannot touch the money path.

Rule (docs/FUTURE.md): the dashboard "sits beside the execution path, never in
it; its failure cannot touch protection". This test enforces it with `ast`, no
runtime. It fails if any module under `src/api/`, or any `src/` module those
modules import (transitively, including function-level imports), can reach:

  1. a write-capable broker or database method NAME (attribute call, bare name
     or imported alias);
  2. a SQL write (INSERT/UPDATE/DELETE/REPLACE/CREATE/DROP/ALTER string
     constant), `.commit()` / `.executescript()`, or a sqlite connect without
     `mode=ro`;
  3. an HTTP write verb (`post`/`put`/`patch`/`delete`/`api_route`/`route`/
     `websocket`/`add_api_route`) on an app or router under `src/api/`.

How the write-capable set is DERIVED (not hand-typed), so a reader can extend it:
  * Broker: every public method of every class in `src/execution/broker.py` and
    `src/execution/broker_parts/` that either (a) starts with a write verb in
    WRITE_PREFIXES, or (b) whose body calls an Alpaca SDK order-write verb in
    SDK_WRITE_CALLS (these are the SDK's own fixed method names).
  * Database: every public method of `class Database` in `src/storage/db.py`
    whose name starts with a WRITE_PREFIXES verb or whose body holds a SQL DML/DDL
    string or calls commit/executescript. `execute` and `commit` are too
    generic to match by name (read-only `conn.execute` is legitimate) and are
    covered by rule 2 instead.
To extend: add a verb to WRITE_PREFIXES / SDK_WRITE_CALLS / SQL_WRITE; the set
re-derives. The defining modules themselves are not scanned for rule 1 (they
define the writes and are imported for their read methods); the reads in
`src/api/` are confined by the name check on `src/api/` itself.

There is no allowlist. The one finding it once recorded (the dashboard's drift
reader lived in `src.coverage_watchdog`, which lazily imports repair/scale-in
code) was cut at the cause: the reader now lives in the stdlib-only
`src.drift_state`. `test_drift_reader_closure_is_read_only` pins that module.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
API = SRC / "api"

WRITE_PREFIXES = (
    "submit_",
    "place_",
    "replace_",
    "cancel_",
    "close_",
    "shift_",
    "insert_",
    "save_",
    "update_",
    "delete_",
    "record_",
    "mark_",
    "persist_",
    "upsert_",
    "write_",
    "purge_",
    "clear_",
    "append_",
    "liquidate_",
)
SDK_WRITE_CALLS = {
    "submit_order",
    "cancel_order_by_id",
    "cancel_orders",
    "replace_order_by_id",
    "close_position",
    "close_all_positions",
}
SQL_WRITE = re.compile(
    r"^\s*(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)|DROP\s+(TABLE|INDEX|VIEW|TRIGGER)|ALTER\s+TABLE)\b",
    re.I,
)
HTTP_WRITE = {"post", "put", "patch", "delete", "api_route", "route", "websocket", "add_api_route"}
# Names with a write-looking prefix that are read-only (verified by body inspection).
# `ensure_diary_dir`-style local helpers are not broker/db methods, so only
# broker/db names ever enter the derived set.

# Each door is "api file -> first tainted module"; the reason is the one-line why it stays.
DOORS: dict[str, str] = {}  # every door is cut; may only shrink, so it stays empty


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _public_methods(tree, only_class=None):
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if only_class and cls.name != only_class:
            continue
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.name.startswith("_"):
                yield fn


def _calls_sdk_write(fn) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr in SDK_WRITE_CALLS for n in ast.walk(fn))


def _has_sql_write(fn) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_WRITE.match(n.value):
            return True
        if isinstance(n, ast.Attribute) and n.attr in {"commit", "executescript"}:
            return True
    return False


def _broker_files():
    return [SRC / "execution" / "broker.py", *sorted((SRC / "execution" / "broker_parts").rglob("*.py"))]


def derive_write_names() -> set[str]:
    names: set[str] = set()
    for p in _broker_files():
        for fn in _public_methods(_parse(p)):
            if fn.name.startswith(WRITE_PREFIXES) or _calls_sdk_write(fn):
                names.add(fn.name)
    for fn in _public_methods(_parse(SRC / "storage" / "db.py"), "Database"):
        if fn.name in {"execute", "commit"}:
            continue
        if fn.name.startswith(WRITE_PREFIXES) or _has_sql_write(fn):
            names.add(fn.name)
    return names


def _module_file(mod: str):
    parts = mod.split(".")
    f = ROOT.joinpath(*parts).with_suffix(".py")
    if f.exists():
        return f
    init = ROOT.joinpath(*parts) / "__init__.py"
    return init if init.exists() else None


def _imports(tree) -> set[str]:
    out = set()
    for n in ast.walk(tree):  # includes function-level (lazy) imports
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return {m for m in out if m == "src" or m.startswith("src.")}


def _defining_files() -> set[Path]:
    return {*_broker_files(), SRC / "storage" / "db.py"}


def reachable_files() -> dict[Path, list[Path]]:
    """Every src file reachable from src/api, mapped to the file that imported it."""
    seen: dict[Path, Path] = {}
    queue = sorted(API.rglob("*.py"))
    for p in queue:
        seen[p] = p
    while queue:
        cur = queue.pop()
        for mod in _imports(_parse(cur)):
            f = _module_file(mod)
            if f and f not in seen and f not in _defining_files():
                seen[f] = cur
                queue.append(f)
    return seen


def _name_hits(tree, write_names):
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in write_names:
            yield n.attr, n.lineno
        elif isinstance(n, ast.Name) and n.id in write_names:
            yield n.id, n.lineno
        elif isinstance(n, ast.alias) and n.name in write_names:
            yield n.name, getattr(n, "lineno", 0)


def _sql_hits(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_WRITE.match(n.value):
            yield f"SQL write string {n.value[:30]!r}", n.lineno
        elif isinstance(n, ast.Attribute) and n.attr in {"commit", "executescript"}:
            yield f".{n.attr}()", n.lineno
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect"
            and getattr(n.func.value, "id", "") == "sqlite3"
        ):
            src = ast.unparse(n)
            if "mode=ro" not in src:
                yield "sqlite3.connect without mode=ro", n.lineno


def _http_hits(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in HTTP_WRITE:
            yield f"HTTP write registration .{n.func.attr}(", n.lineno


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def test_write_set_is_derived_and_nonempty():
    names = derive_write_names()
    for must in (
        "submit_order",
        "cancel_entry_order",
        "close_position",
        "replace_stop_loss",
        "insert_trade",
        "update_open_stop_loss",
    ):
        assert must in names, f"derivation missed known write method {must}"


def _write_hits() -> set[str]:
    write_names = derive_write_names()
    return {f"{_rel(f)}::{name}" for f in reachable_files() for name, _ in _name_hits(_parse(f), write_names)}


def test_api_cannot_reach_write_capable_names():
    new = sorted(_write_hits())
    assert not new, "src/api can reach the money path:\n" + "\n".join(new)


def test_api_has_no_database_write():
    bad = [
        f"{_rel(f)}:{ln} {what}"
        for f in reachable_files()
        if f.is_relative_to(API)
        for what, ln in _sql_hits(_parse(f))
    ]
    assert not bad, "src/api can write to the database:\n" + "\n".join(bad)


def test_api_registers_no_http_write_verb():
    bad = [f"{_rel(f)}:{ln} {what}" for f in sorted(API.rglob("*.py")) for what, ln in _http_hits(_parse(f))]
    assert not bad, "src/api registers a write route:\n" + "\n".join(bad)


def _closure(start: Path) -> set[Path]:
    seen, queue = {start}, [start]
    while queue:
        for mod in _imports(_parse(queue.pop())):
            f = _module_file(mod)
            if f and f.resolve() not in seen:
                seen.add(f.resolve())
                queue.append(f.resolve())
    return seen


def test_drift_reader_closure_is_read_only():
    """The modules the dashboard reads drift state and trading days from reach nothing in src/execution."""
    import src.drift_state as ds
    import src.trading_day as td

    for mod in (ds, td):
        seen = _closure(Path(mod.__file__).resolve())
        bad = sorted(_rel(f) for f in seen if f.is_relative_to(SRC / "execution") or f.name == "coverage_watchdog.py")
        assert not bad, f"{mod.__name__} reaches the money path:\n" + "\n".join(bad)


def tainted_doors() -> set[str]:
    """Edges api -> non-api module from which some allow-listed write-capable file is reachable."""
    reach = reachable_files()
    edges = {f: {m for m in (_module_file(i) for i in _imports(_parse(f))) if m in reach} for f in reach}
    hit_names = {h.split("::")[0] for h in _write_hits()}
    hit_files = {f for f in reach if _rel(f) in hit_names}
    tainted = set(hit_files)
    grew = True
    while grew:
        grew = False
        for f, outs in edges.items():
            if f not in tainted and outs & tainted:
                tainted.add(f)
                grew = True
    return {
        f"{_rel(f)} -> {_rel(n)}"
        for f in edges
        if f.is_relative_to(API)
        for n in edges[f]
        if n in tainted and not n.is_relative_to(API)
    }


def test_api_has_no_new_door_into_the_money_path():
    new = sorted(tainted_doors() - set(DOORS))
    assert not new, "src/api gained a NEW import route toward write-capable code:\n" + "\n".join(new)


def test_doors_only_shrink():
    stale = sorted(set(DOORS) - tainted_doors())
    assert not stale, "remove these cut doors from DOORS (it may only shrink):\n" + "\n".join(stale)
