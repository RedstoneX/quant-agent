"""Each module lifted out of `ExecutionStage._run_session` imports on its own.

`src/stage_execution.py` imports these at module load, so an import error in
one would take the whole execution stage down; importing each directly here
names the broken module instead of failing somewhere downstream.
"""

import importlib

import pytest

MODULES = [
    ("src.stage_execution_parts.protect_entry_stops", "protect_pending_entry_stops"),
    ("src.stage_execution_parts.cover_loop", "cover_qty_and_label"),
    ("src.stage_execution_parts.cover_loop", "await_cover_and_finalize"),
    ("src.stage_execution_parts.state", "EntryLeg"),
]


@pytest.mark.parametrize(("module", "name"), MODULES)
def test_part_imports_directly(module, name):
    mod = importlib.import_module(module)
    assert callable(getattr(mod, name))
