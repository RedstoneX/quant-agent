## Item 192 (RETIRED 2026-09-30) — local interpreter pinned to CI's
Retired because all three DONE WHEN criteria are satisfied on main, not because
the item was abandoned.
- `.python-version` on main reads `3.11`, and both CI jobs read it via
  `python-version-file` rather than each naming a version.
- A local pytest run aborts and names both versions when the running
  interpreter is not the pinned one (shipped in #792).
- The last open criterion — actually rebuilding the dev `.venv`, which measured
  3.12.3 — was completed 2026-09-30: `pip install uv`, `uv python install 3.11`,
  `uv venv --python 3.11`. `/home/ubuntu/projects/quant-agent/.venv` now measures
  **Python 3.11.16** [measured: `.venv/bin/python -V`]. The previous interpreter
  is preserved at `.venv312` so any session mid-run on it is not broken.
Why it mattered: the split was the direct cause of two confident, wrong agent
diagnoses in one session. A prompt-drift check hashed `ast.dump()` of a parsed
function, 3.12 changed that output, and identical source hashed differently
locally and in CI.
## Item 192 (RETIRED 2026-09-30) — local interpreter pinned to CI's
Retired because all three DONE WHEN criteria are satisfied on main, not because
the item was abandoned.
- `.python-version` on main reads `3.11`, and both CI jobs read it via
  `python-version-file` rather than each naming a version.
- A local pytest run aborts and names both versions when the running
  interpreter is not the pinned one (shipped in #792).
- The last open criterion — actually rebuilding the dev `.venv`, which measured
  3.12.3 — was completed 2026-09-30: `pip install uv`, `uv python install 3.11`,
  `uv venv --python 3.11`. `/home/ubuntu/projects/quant-agent/.venv` now measures
  **Python 3.11.16** [measured: `.venv/bin/python -V`]. The previous interpreter
  is preserved at `.venv312` so any session mid-run on it is not broken.
Why it mattered: the split was the direct cause of two confident, wrong agent
diagnoses in one session. A prompt-drift check hashed `ast.dump()` of a parsed
function, 3.12 changed that output, and identical source hashed differently
locally and in CI.
## item 192 — detail moved from the board 2026-09-30

CI runs 3.11 (`.github/workflows/test.yml`); the checked-in dev `.venv` measured 3.12.3, and nothing anywhere pinned or checked the two against each other. The drift already cost real time once: a prompt-drift check hashed `ast.dump()` of a parsed function, and Python 3.12 added a `type_params` field to `FunctionDef`/`AsyncFunctionDef`/`ClassDef` that 3.11 doesn't have, so the same unchanged source hashed differently under the two interpreters — CI went red, local ran green, and two agents produced confident but wrong diagnoses before the version skew itself was found.


