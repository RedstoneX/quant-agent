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
