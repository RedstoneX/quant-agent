"""Board item 202: every place src/ can reach a provider is a NAMED replay seam.

A replay of a recorded session must be deterministic and free. The runtime wall
(`ops/rehearsal/network_wall.py`) catches an outbound attempt when it happens;
this guard catches it when it is WRITTEN. Any source file that imports an HTTP,
socket or provider-SDK client is recorded in `replay_outbound_sites_baseline.json`
(measured 2026-10-02 by AST scan of src/; the current state is adopted, so the
guard is green on arrival). A NEW file or a NEW client module in a file fails
here until it is handled in the rehearsal: served from a recording by the
patch-where-the-client-is-built pattern, or refused loudly naming the missing
recording (see `ops/rehearsal/feed_recording.py`, `market_recording.py`).
Then add it to the baseline in the same change. The baseline is SHRINK-ONLY in
spirit: an entry whose import has gone must be removed, so it cannot go stale.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("replay_outbound_sites_baseline.json")

CLIENT_MODULES = {
    "requests", "httpx", "urllib", "urllib3", "aiohttp", "curl_cffi", "yfinance",
    "fredapi", "openai", "anthropic", "websockets", "websocket", "http", "smtplib",
    "ftplib", "telegram", "alpaca", "alpaca_trade_api", "feedparser",
    "pandas_datareader", "socket", "ssl",
}


def scan(src: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(src.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hits |= {a.name.split(".")[0] for a in node.names} & CLIENT_MODULES
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in CLIENT_MODULES:
                    hits.add(node.module.split(".")[0])
        if hits:
            found[path.relative_to(src.parent).as_posix()] = sorted(hits)
    return found


def _diff(found, baseline):
    new = {f: sorted(set(m) - set(baseline.get(f, []))) for f, m in found.items()
           if set(m) - set(baseline.get(f, []))}
    gone = {f: sorted(set(m) - set(found.get(f, []))) for f, m in baseline.items()
            if set(m) - set(found.get(f, []))}
    return new, gone


def test_no_new_outbound_client_site_without_a_replay_seam():
    new, gone = _diff(scan(ROOT / "src"), json.loads(BASELINE.read_text()))
    assert not new, (
        "NEW outbound-client import(s) in src/ that a replay has no named seam for: "
        f"{new}. A replay must be served from a recording or refuse naming the "
        "missing recording, never reach a live provider (board item 202). Handle "
        "it in ops/rehearsal, then add it to tests/replay_outbound_sites_baseline.json."
    )


def test_baseline_has_no_stale_entries():
    new, gone = _diff(scan(ROOT / "src"), json.loads(BASELINE.read_text()))
    assert not gone, (
        f"baseline lists client imports that no longer exist: {gone}. "
        "Remove them from tests/replay_outbound_sites_baseline.json (shrink-only)."
    )


def test_the_guard_detects_a_new_site(tmp_path):
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "leaky.py").write_text("import requests\n\ndef f():\n    return requests.get('http://x')\n")
    new, _ = _diff(scan(pkg), {})
    assert new == {"src/leaky.py": ["requests"]}
