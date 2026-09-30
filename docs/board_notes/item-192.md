## item 192 — detail moved from the board 2026-09-30

CI runs 3.11 (`.github/workflows/test.yml`); the checked-in dev `.venv` measured 3.12.3, and nothing anywhere pinned or checked the two against each other. The drift already cost real time once: a prompt-drift check hashed `ast.dump()` of a parsed function, and Python 3.12 added a `type_params` field to `FunctionDef`/`AsyncFunctionDef`/`ClassDef` that 3.11 doesn't have, so the same unchanged source hashed differently under the two interpreters — CI went red, local ran green, and two agents produced confident but wrong diagnoses before the version skew itself was found.


