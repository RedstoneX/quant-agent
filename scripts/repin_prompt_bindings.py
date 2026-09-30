"""Re-pin the behaviour-change registry after you have re-read the prose.

    python -m scripts.repin_prompt_bindings          # rewrite the digests
    python -m scripts.repin_prompt_bindings --check  # report, change nothing

Run this only AFTER reading the prompt prose a failing binding points at
and either fixing it or satisfying yourself it is still true. Re-pinning
first and reading never is the one way to defeat the check; it is a
convention at that edge, exactly like the deletion-site registry it sits
beside (`src/retired_mechanisms.py`).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.prompt_bindings import (  # noqa: E402
    REGISTRY_PATH,
    REPO_ROOT,
    check,
    current_digests,
    load_registry,
)


def repin(registry_path: Path = REGISTRY_PATH) -> list[str]:
    """Rewrite each `code_digest:`/`prose_digest:` in place. Returns changes.

    Edits the YAML as text rather than round-tripping it through the
    parser, because the file's comments are half its value and
    `yaml.safe_dump` would drop every one of them.
    """
    digests = current_digests(REPO_ROOT, registry_path)
    order = [b.name for b in load_registry(registry_path)]
    text = registry_path.read_text()
    changed: list[str] = []

    # Walk the entries in file order; each `- name:` opens the next one.
    blocks = re.split(r"(?m)^(?=  - name: )", text)
    out: list[str] = []
    index = 0
    for block in blocks:
        match = re.match(r"  - name: (\S+)", block)
        if match is None:
            out.append(block)
            continue
        name = match.group(1)
        if name != order[index]:  # pragma: no cover - parser/regex disagreement
            raise SystemExit(
                f"registry order disagrees with the text scan at {name!r}; "
                f"fix the file by hand",
            )
        index += 1
        code, prose = digests[name]
        for key, value in (("code_digest", code), ("prose_digest", prose)):
            pattern = re.compile(rf'(?m)^(    {key}: ")([0-9a-f]+)(")$')
            found = pattern.search(block)
            if found is None:
                raise SystemExit(f"{name}: no `{key}:` line to re-pin")
            if found.group(2) != value:
                changed.append(f"{name}.{key}: {found.group(2)} -> {value}")
            block = pattern.sub(rf'\g<1>{value}\g<3>', block, count=1)
        out.append(block)
    registry_path.write_text("".join(out))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report only")
    args = parser.parse_args()
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
            print()
        print(f"{len(problems)} binding(s) out of agreement")
        return 1 if problems else 0
    changes = repin()
    for change in changes:
        print(change)
    print(f"re-pinned {len(changes)} digest(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
