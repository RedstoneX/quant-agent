"""Owner alerts go through the retry funnel: no bare ``notifier.send`` in ``src/``.

``send_owner_alert`` retries a failed send and writes a counted undelivered row
(``src/notifier/owner_alert_delivery.deliver_with_retry``). A caller that calls
``notifier.send(...)`` itself gets none of that: one attempt, and silence when
it fails. That is how the "NO STOP AT ALL" page -- the canonical owner alert the
funnel was built for -- could be lost to a single Telegram blip with no trace.

The guard is ABSOLUTE, not shrink-only: every ``<expr>.send(...)`` in ``src/``
whose receiver mentions ``notifier`` must sit in an allow-listed module, and
each allow-list entry carries the reason it is not an owner alert. Anything new
fails until it either routes through the funnel or is justified here.

Run: ``python -m scripts.owner_alert_funnel_guard``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Module -> why a bare send there is not an unretried owner alert.
ALLOWED: dict[str, str] = {
    "src/notifier/owner_alert_delivery.py":
        "the funnel itself -- this IS the retry loop",
    "src/cost_circuit/alert_outcome.py":
        "three-way delivered/suppressed/failed outcome with its own durable "
        "retryable alert_state row; a failure stays at state 0 and is re-sent",
    "src/scheduler.py":
        "the routine end-of-session summary, not a money-at-risk alarm; the "
        "alarms inside a session raise their own send_owner_alert",
}

#: Receiver expressions naming a notifier. A bare local called `n` is not
#: matched; the convention in this tree is `notifier` / `self.notifier`.
_MARKER = "notifier"


def _receiver_text(node: ast.Attribute) -> str:
    try:
        return ast.unparse(node.value)
    except Exception:  # noqa: BLE001  (older grammar corner)
        return ""


def scan_sites(text: str) -> list[tuple[int, str]]:
    """Every ``<...notifier...>.send(...)`` call site as (line, receiver)."""
    sites: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr != "send":
            continue
        recv = _receiver_text(fn)
        if _MARKER in recv.lower():
            sites.append((node.lineno, recv))
    return sites


def scanned_paths() -> list[str]:
    return sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
    )


def violations() -> list[str]:
    bad: list[str] = []
    for path in scanned_paths():
        if path in ALLOWED:
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            sites = scan_sites(text)
        except SyntaxError:
            continue
        for line, recv in sites:
            bad.append(f"{path}:{line}: `{recv}.send(...)` bypasses the retry funnel")
    return sorted(bad)


def stale_allowances() -> list[str]:
    """Allow-list entries whose file is gone or no longer holds a bare send."""
    stale: list[str] = []
    for path in sorted(ALLOWED):
        full = ROOT / path
        if not full.exists():
            stale.append(f"{path}: allow-listed but the file does not exist")
            continue
        try:
            if not scan_sites(full.read_text(encoding="utf-8", errors="replace")):
                stale.append(f"{path}: allow-listed but holds no notifier send")
        except SyntaxError:
            continue
    return stale


def main() -> int:
    bad = violations() + stale_allowances()
    if bad:
        print(
            "owner alerts must go through deliver_with_retry "
            "(src/notifier/owner_alert_delivery.py), not a bare notifier.send; "
            "allow-list the site in scripts/owner_alert_funnel_guard.ALLOWED "
            "with a reason if it is genuinely not an owner alert:\n"
            + "\n".join(bad),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
