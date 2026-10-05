"""Board item 202: every place ``src/`` can reach a provider is a NAMED replay seam.

A replay of a recorded session must be deterministic and free. The runtime wall
(``ops/rehearsal/network_wall.py``) catches an outbound attempt when it happens;
this guard catches it when it is WRITTEN. Any source file that imports an HTTP,
socket or provider-SDK client is an outbound site, and a NEW site — a new file,
or a new client module inside an existing file — must first be handled in the
rehearsal: served from a recording by the patch-where-the-client-is-built
pattern, or refused loudly naming the missing recording (see
``ops/rehearsal/feed_recording.py``, ``market_recording.py``).

NO STORED BASELINE
------------------
The first cut of this guard kept its "before" picture in
``tests/replay_outbound_sites_baseline.json`` — an AST scan of ``src/`` frozen
on 2026-10-02 and committed. That is a cached measurement, not policy: nothing
in it was reasoned about by a human, every entry was produced by the scanner
below, and every change that touched a listed file had to edit the one shared
file that every other open change was also editing.

So this stores nothing (docs/GUARDS_WITHOUT_STORED_STATE.md). At check time it
scans the working tree, scans ``origin/main`` separately, and reports the
DELTA. If ``origin/main`` cannot be read it REFUSES; it never passes by default.

``CLIENT_MODULES`` below is the one thing that IS policy — the list of module
names that mean "this code can leave the box" — and it stays in code, where it
is reviewed. It is a definition, not a record of the current state.

Run it directly: ``python -m scripts.replay_outbound_guard``.
"""
from __future__ import annotations

import ast
import sys

from scripts.guard_reference import (
    ROOT,
    ReferenceUnavailable,
    TRUNK,
    trunk_blobs,
    working_paths,
)

SCAN_DIR = "src"

CLIENT_MODULES = {
    "requests", "httpx", "urllib", "urllib3", "aiohttp", "curl_cffi", "yfinance",
    "fredapi", "openai", "anthropic", "websockets", "websocket", "http", "smtplib",
    "ftplib", "telegram", "alpaca", "alpaca_trade_api", "feedparser",
    "pandas_datareader", "socket", "ssl",
}


# Submodules of a client package that open no connection. Each is named, with
# its reason; there is no wildcard and no count. A module not listed here is a
# client, so a NEW outbound import in the same package is still refused.
NOT_CLIENT_SUBMODULES = {
    "urllib.parse": "string splitting and quoting of URLs; performs no I/O",
    "alpaca.trading.requests": "pydantic request models (data classes); no client",
    "alpaca.trading.enums": "plain enums; no client",
}

# Attributes of a client module whose use alone opens nothing, by identity.
NOT_CLIENT_ATTRIBUTES = {
    "socket": {
        "setdefaulttimeout": "sets a process default; opens no socket",
        "getdefaulttimeout": "reads the process default; opens no socket",
    },
}


def _excluded_submodule(dotted: str) -> bool:
    return any(dotted == m or dotted.startswith(m + ".") for m in NOT_CLIENT_SUBMODULES)


def _only_harmless_attributes(tree: ast.AST, name: str) -> bool:
    """True when every use of ``name`` is an attribute listed as not a client."""
    allowed = NOT_CLIENT_ATTRIBUTES.get(name, {})
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            up = parents.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node and up.attr in allowed):
                return False
    return True


def scan_text(text: str) -> set[str]:
    """Every client module one source file imports, at any nesting depth."""
    tree = ast.parse(text)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in CLIENT_MODULES or _excluded_submodule(a.name):
                    continue
                if a.asname is None and root in NOT_CLIENT_ATTRIBUTES and a.name == root:
                    if _only_harmless_attributes(tree, root):
                        continue
                hits.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root not in CLIENT_MODULES or _excluded_submodule(node.module):
                continue
            allowed = NOT_CLIENT_ATTRIBUTES.get(root, {})
            if node.module == root and allowed and all(a.name in allowed for a in node.names):
                continue
            hits.add(root)
    return hits


def scanned_paths() -> list[str]:
    """Tracked ``.py`` files under ``src/`` in the working tree."""
    return working_paths(f"{SCAN_DIR}/*.py")


def working_sites() -> dict[str, set[str]]:
    """Outbound-client imports per file in this working tree."""
    out: dict[str, set[str]] = {}
    for path in scanned_paths():
        try:
            hits = scan_text((ROOT / path).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # unparsable source is a different failure, caught elsewhere
        if hits:
            out[path] = hits
    return out


def trunk_sites(paths: list[str]) -> dict[str, set[str]]:
    """The same measurement taken on ``origin/main`` at check time.

    A path absent from the trunk simply has no sites — that is how a new file
    is recognised. A trunk blob that will not parse is unmeasurable, so the
    guard refuses rather than treat it as clean.
    """
    out: dict[str, set[str]] = {}
    for path, text in trunk_blobs(paths).items():
        try:
            hits = scan_text(text)
        except SyntaxError as exc:
            raise ReferenceUnavailable(
                f"cannot parse {TRUNK}:{path} ({exc}), so this guard cannot measure "
                f"what that file already imported; it refuses rather than pass."
            ) from exc
        if hits:
            out[path] = hits
    return out


def violations() -> list[str]:
    """Every file this working tree gave an outbound client the trunk had not."""
    now = working_sites()
    before = trunk_sites(scanned_paths())
    bad: list[str] = []
    for path, hits in sorted(now.items()):
        added = sorted(hits - before.get(path, set()))
        if added:
            had = sorted(before.get(path, set())) or ["nothing"]
            bad.append(f"{path}: +{added} (on {TRUNK} this file imported {had})")
    return bad


def main(argv: list[str] | None = None) -> int:
    try:
        bad = violations()
    except ReferenceUnavailable as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(
            "NEW outbound-client import(s) in src/ that a replay has no named seam "
            "for. A replay must be served from a recording or refuse naming the "
            "missing recording, never reach a live provider (board item 202). "
            "Handle it in ops/rehearsal first. Added against %s:\n%s"
            % (TRUNK, "\n".join(bad)),
            file=sys.stderr,
        )
        return 1
    print(f"replay outbound guard: this tree adds no new outbound site against {TRUNK}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
