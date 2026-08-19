"""Stage 2 Checkpoint C — structural safety regression tests for the "Thin
Read-Only Mission Control API" under `src/api/`.

These tests are purely static/structural: source-level AST scans of
`src/api/*.py` (and a handful of trading-critical files, checked in the
reverse direction) plus one in-process ASGI smoke test of the app's
GET-only enforcement middleware. None of them seed the DB, hit the network,
or monkeypatch config — that live-behavior coverage lives in a sibling test
file. The goal here is to make it structurally impossible (and loudly
test-visible if it ever becomes possible) for `src/api/` to place, cancel,
or modify a broker order, write to the trading SQLite DB, or import the
trading-execution stack.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
API_DIR = REPO_ROOT / "src" / "api"


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _api_source_files() -> list[Path]:
    return sorted(API_DIR.glob("*.py"))


# ---------------------------------------------------------------------------
# 1. Every registered route is GET/HEAD/OPTIONS only.
# ---------------------------------------------------------------------------


def test_api_routes_are_get_only():
    from src.api.server import app

    checked_any = False
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        checked_any = True
        assert methods <= {"GET", "HEAD", "OPTIONS"}, (
            f"route {getattr(route, 'path', route)!r} allows non-safe methods: {methods}"
        )
    assert checked_any, "expected at least one route with a .methods attribute to check"


# ---------------------------------------------------------------------------
# 2. No write-capable broker method is ever referenced as an attribute
#    access anywhere in src/api/ source (AST-level, not substring).
# ---------------------------------------------------------------------------

_FORBIDDEN_BROKER_ATTRS = {
    "submit_order",
    "cancel_order",
    "cancel_order_by_id",
    "cancel_open_orders",
    "cancel_protective_stops",
    "cancel_snapshotted_stops",
    "cancel_open_entry_orders",
    "close_position",
    "place_entry_protection",
    "_restore_stop_orders",
    "_finalize_pending_protections",
    # Independent Stage 2 review (2026-08-09): both write-capable (cancel +
    # resubmit a stop order) and originally missing from this denylist —
    # neither is referenced anywhere in src/api/ today, but a future
    # "current effective stop" read handler could call one of these without
    # this test catching it.
    "shift_stops_down",
    "replace_stop_loss",
}


@pytest.mark.parametrize("path", _api_source_files(), ids=lambda p: p.name)
def test_no_write_capable_broker_calls_in_api_source(path: Path):
    tree = _parse(path)
    found_attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    offenders = found_attrs & _FORBIDDEN_BROKER_ATTRS
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} references forbidden write-capable "
        f"broker attribute(s) as an ast.Attribute node: {offenders}"
    )


# ---------------------------------------------------------------------------
# 3. AlpacaBroker is constructed in exactly one place: broker_reads.py's
#    _get_broker(). No other file under src/api/ instantiates it.
# ---------------------------------------------------------------------------


def _call_target_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_alpaca_broker_constructed_only_in_broker_reads():
    construction_sites: list[Path] = []
    for path in _api_source_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_target_name(node) == "AlpacaBroker":
                construction_sites.append(path)
                break
    assert construction_sites == [API_DIR / "broker_reads.py"], (
        f"AlpacaBroker(...) constructed outside broker_reads.py: "
        f"{[p.relative_to(REPO_ROOT) for p in construction_sites]}"
    )


# ---------------------------------------------------------------------------
# 4. Trading-critical modules must never import src.api (that direction
#    would be a real coupling hazard: the API process is not supposed to be
#    load-bearing for trading).
# ---------------------------------------------------------------------------

_TRADING_CRITICAL_FILES = [
    "main.py",
    "src/pipeline.py",
    "src/pipeline_stages.py",
    "src/execution/broker.py",
    "src/risk/rules.py",
    "src/scheduler.py",
]


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


@pytest.mark.parametrize("rel_path", _TRADING_CRITICAL_FILES)
def test_api_package_never_imports_trading_critical_modules(rel_path: str):
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"expected trading-critical file to exist: {path}"
    tree = _parse(path)
    imported = _imported_module_names(tree)
    offenders = [m for m in imported if m == "src.api" or m.startswith("src.api.")]
    assert not offenders, (
        f"{rel_path} imports src.api, which trading-critical code must never "
        f"depend on: {offenders}"
    )


# ---------------------------------------------------------------------------
# 5. src/api/ never imports the pipeline/risk stack, and never imports the
#    write-capable Database class from src.storage.db.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_PREFIXES = ("src.pipeline", "src.pipeline_stages", "src.risk")


@pytest.mark.parametrize("path", _api_source_files(), ids=lambda p: p.name)
def test_api_source_files_never_import_pipeline_or_risk(path: Path):
    tree = _parse(path)
    imported = _imported_module_names(tree)
    offenders = [
        m for m in imported
        if any(m == prefix or m.startswith(prefix + ".") for prefix in _FORBIDDEN_IMPORT_PREFIXES)
    ]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports forbidden pipeline/risk module(s): {offenders}"
    )

    # Sibling assertion, same test: no file under src/api/ may import the
    # read/WRITE Database class from src.storage.db (db_reads.py is
    # supposed to open its own independent mode=ro connection instead).
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.storage.db":
            imported_names = {alias.name for alias in node.names}
            assert "Database" not in imported_names, (
                f"{path.relative_to(REPO_ROOT)} imports src.storage.db.Database "
                f"(the write-capable class) directly"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "src.storage.db", (
                    f"{path.relative_to(REPO_ROOT)} does `import src.storage.db` directly"
                )


# ---------------------------------------------------------------------------
# 6. No SQL write statement is ever passed to conn.execute(...) in
#    db_reads.py — every execute() call's literal SQL argument must be a
#    SELECT (checked via AST on the string/f-string literal, not a raw
#    substring scan of the whole file, so the module's own docstring
#    prose about INSERT/UPDATE/DELETE/commit() can't cause a false
#    positive or a false negative).
# ---------------------------------------------------------------------------


def _literal_sql_text(node: ast.AST) -> str | None:
    """Best-effort reconstruction of the literal text of a string/f-string
    argument, ignoring any interpolated `{where}`-style expressions (those
    are always parameter-free structural fragments built from constants
    in this module, never externally-controlled write SQL)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts)
    return None


def test_no_sql_write_statements_in_db_reads():
    path = API_DIR / "db_reads.py"
    tree = _parse(path)

    execute_calls = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            sql_text = _literal_sql_text(node.args[0])
            if sql_text is None:
                continue
            execute_calls += 1
            stripped = sql_text.strip()
            assert stripped.upper().startswith("SELECT") or stripped.upper().startswith("PRAGMA"), (
                f"conn.execute(...) call in db_reads.py does not start with "
                f"SELECT/PRAGMA: {sql_text!r}"
            )
    assert execute_calls > 0, "expected to find at least one conn.execute(...) call to check"

    # Belt-and-suspenders: also confirm no .commit() call site exists at all.
    commit_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "commit"
    ]
    assert not commit_calls, "db_reads.py should never call .commit()"


# ---------------------------------------------------------------------------
# 7. db_reads.py never imports the shared write-capable Database class.
#    (Explicit, narrowly-named duplicate of part of test 5, kept because
#    it documents the specific invariant db_reads.py's own docstring calls
#    out by name.)
# ---------------------------------------------------------------------------


def test_no_db_write_calls_via_shared_database_class():
    path = API_DIR / "db_reads.py"
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.storage.db":
            imported_names = {alias.name for alias in node.names}
            assert "Database" not in imported_names
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "src.storage.db"


# ---------------------------------------------------------------------------
# 8. db_reads.py's connection helper actually uses the read-only SQLite URI
#    mode, not just a plan mentioned in a docstring.
# ---------------------------------------------------------------------------


def test_read_only_sqlite_connection_uses_mode_ro():
    text = (API_DIR / "db_reads.py").read_text()
    assert "mode=ro" in text
    assert "uri=True" in text


# ---------------------------------------------------------------------------
# 9. Behavioral smoke test: the app-level GET-only middleware actually
#    rejects non-safe methods with 405, before any route handler (and thus
#    before any config/DB access) runs.
# ---------------------------------------------------------------------------


def test_server_has_get_only_enforcement_middleware():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/health").status_code == 405
    assert client.put("/account").status_code == 405
    assert client.delete("/trades").status_code == 405
    assert client.patch("/positions").status_code == 405


# ---------------------------------------------------------------------------
# 10. Stage 3 cockpit: the `/ui` static mount is covered by the SAME
#     app-level GET-only middleware as the JSON routes above (it wraps the
#     whole ASGI app regardless of mount registration order) and cannot be
#     used to escape `src/api/static/` via path traversal. Pins the
#     behavior verified by hand during the Stage 3 review so a future
#     middleware/mount-ordering change or Starlette upgrade can't silently
#     regress it.
# ---------------------------------------------------------------------------


def test_ui_static_mount_is_get_only_and_has_no_path_traversal():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/ui/app.js").status_code == 405
    assert client.put("/ui/index.html").status_code == 405
    assert client.delete("/ui/styles.css").status_code == 405

    get_resp = client.get("/ui/index.html")
    assert get_resp.status_code == 200

    traversal_resp = client.get("/ui/../server.py")
    assert traversal_resp.status_code in (403, 404)


def test_cockpit_static_mount_is_get_only_and_has_no_path_traversal():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/cockpit/app.js").status_code == 405
    assert client.put("/cockpit/index.html").status_code == 405
    assert client.delete("/cockpit/styles.css").status_code == 405

    get_resp = client.get("/cockpit/index.html")
    assert get_resp.status_code == 200

    traversal_resp = client.get("/cockpit/../server.py")
    assert traversal_resp.status_code in (403, 404)
