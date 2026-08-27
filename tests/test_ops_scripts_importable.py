"""Every module under `ops/` and `scripts/` must still import.

`pytest` collects `tests/` only. `ops/` and `scripts/` hold the operational
tooling — the model benchmark, the commissioning verifier, the pricing check,
the replay and preview utilities — and NOTHING exercised them, so a schema
change in `src/models.py` could break one and the suite would stay green.

That is not hypothetical. On 2026-08-27 the Phase 1 tranche (`138edd2`) made
`setup_type`, `expected_horizon_sessions` and a structural level required on
`TechAnalysisResult`, and `ops/model_policy/scenarios.py` stopped importing
the same day. It was found by hand, not by any test. The tool it broke is the
one `docs/architecture/MODEL_ROUTING_POLICY.md` names as the thing that
re-derives the model-routing decision from scratch, so the failure was silent
in exactly the place where being wrong is expensive.

Three separate consumers drifted the same way that day — the benchmark
scenarios, `ops/preview/branch_preview.py` and the Mission Control evidence
route all read `TargetPosition.target_weight_pct` after Phase 2b made it
optional. Patching each one as it was discovered fixes the instances and not
the class. This is the guard for the class.

The module list is DISCOVERED, never hardcoded: a static list would itself go
stale, which is the same failure one level up. A new file under `ops/` is
covered the moment it is added, with no one having to remember.

Import is a deliberately shallow check. It does not prove a tool works — it
proves the tool still agrees with the schemas it is built on, which is the
thing that silently rots. Anything import-time is caught: module-level
fixtures constructed from `src.models`, renamed or deleted functions, moved
modules.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Modules excluded from the import sweep, each with the reason. Keep this
#: empty if at all possible — an entry here is a module nothing protects.
_EXCLUDED: dict[str, str] = {}


def _discover() -> list[str]:
    """Every importable module path under `ops/` and `scripts/`."""
    found: list[str] = []
    for package in ("ops", "scripts"):
        root = PROJECT_ROOT / package
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.name == "__init__.py" or "__pycache__" in path.parts:
                continue
            dotted = ".".join(path.relative_to(PROJECT_ROOT).with_suffix("").parts)
            if dotted not in _EXCLUDED:
                found.append(dotted)
    return found


_MODULES = _discover()


def test_the_sweep_actually_found_modules():
    """A discovery bug that silently found nothing would make every test below
    vacuously pass — the exact failure mode this file exists to prevent."""
    assert len(_MODULES) >= 5, f"only discovered {_MODULES!r}"


@pytest.mark.parametrize("module_path", _MODULES)
def test_module_imports(module_path: str):
    """Fails the moment a `src/` schema change outruns an operational tool.

    If this fails, the fix is to update the tool — not to add the module to
    `_EXCLUDED`. Excluding it restores exactly the blind spot that let the
    benchmark harness break unnoticed.
    """
    assert importlib.import_module(module_path) is not None
