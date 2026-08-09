#!/usr/bin/env python3
"""Deterministic QAMC Claude Code guardrails.

This is intentionally small. It blocks only operations that are never part of the
current Claude workflow; judgment-heavy rules remain in project instructions/review.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def deny(reason: str) -> None:
    print(reason, file=sys.stderr)
    raise SystemExit(2)


def _mentions_secret_env(command: str) -> bool:
    """Best-effort shell guard; the sandbox provides the OS-level read boundary."""
    scrubbed = command.lower().replace(".env.example", "")
    return bool(re.search(r"(?<![a-z0-9_])\.env(?:\.[a-z0-9_.-]+)?(?:\b|$)", scrubbed))


def bash_guard(command: str) -> None:
    lowered = command.lower()

    # ChatGPT/operator governance owns accepted merges and main publication.
    if re.search(r"\bgh\s+pr\s+merge\b", lowered):
        deny("QAMC guard: Claude Code must not merge pull requests.")
    if re.search(r"\bgh\s+repo\s+delete\b", lowered):
        deny("QAMC guard: repository deletion is not allowed.")
    if re.search(r"\bgit\s+reset\s+--hard\b", lowered):
        deny("QAMC guard: destructive hard reset is not allowed.")
    if re.search(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)(?:\s|$)", lowered):
        deny("QAMC guard: force-push is not allowed.")

    # Block direct pushes whose refspec targets main/master. Feature branches remain allowed.
    if re.search(r"\bgit\s+push\b[^\n]*(?:\s|:)(?:refs/heads/)?(?:main|master)(?:\s|$)", lowered):
        deny("QAMC guard: direct push to main/master is not allowed.")

    # Read/Edit permissions plus sandbox denyRead are authoritative; this catches obvious
    # shell attempts too, while still allowing the committed non-secret .env.example.
    if _mentions_secret_env(command):
        deny("QAMC guard: secret-bearing .env files are not available to Claude Code.")


def write_guard(tool_name: str, tool_input: dict) -> None:
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    path = str(Path(raw_path)).replace("\\", "/")
    base = os.path.basename(path)

    if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
        deny("QAMC guard: Claude Code must not write secret-bearing environment files.")

    if "/secrets/" in f"/{path.strip('/')}/" or path.endswith("/secrets"):
        deny("QAMC guard: Claude Code must not write secret files.")

    if path.endswith("/config/credentials.json") or path == "config/credentials.json":
        deny("QAMC guard: Claude Code must not write credential files.")

    if path.endswith("/config/settings.yaml") or path.endswith("config/settings.yaml"):
        new_text = ""
        if tool_name == "Write":
            new_text = str(tool_input.get("content", ""))
        elif tool_name == "Edit":
            new_text = str(tool_input.get("new_string", ""))
        if re.search(r"(?mi)^\s*paper\s*:\s*false\b", new_text):
            deny("QAMC guard: Alpaca paper mode cannot be changed to false.")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Fail open on malformed hook input rather than breaking development.
        return

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name == "Bash":
        bash_guard(str(tool_input.get("command", "")))
    elif tool_name in {"Edit", "Write"}:
        write_guard(tool_name, tool_input)


if __name__ == "__main__":
    main()
