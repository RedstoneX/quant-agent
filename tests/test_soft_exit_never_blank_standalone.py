"""Witness: the never-blank module builds and runs from stubs, alone.

Run in a fresh interpreter so the big owner modules are provably never
loaded: if the module needed them (or closed a cycle through them) the
subprocess would fail or `sys.modules` would show them.
"""

import pathlib
import subprocess
import sys
import textwrap

_PROBE = textwrap.dedent("""
    import sys
    from src import soft_exit_never_blank as m

    class _R:
        def __init__(self, payload): self.payload = payload
        def parse_json(self): return self.payload

    class _D:
        targets = [{"symbol": "ABC", "thesis_invalid_if": ""}]

    d, rec, logs = _D(), [], []
    class _L:
        def info(self, *a): logs.append(a)
        def error(self, *a): logs.append(a)
    raw = _R({"targets": [{"symbol": "abc", "thesis_invalid_if": "closes below the 50-day"}]})
    left = m.apply_mechanical_heal(d, raw, ["ABC", "XYZ"], lambda *a: rec.append(a), _L())
    assert left == ["XYZ"], left
    assert rec and rec[0][1] == "mechanical", rec
    assert d.targets[0]["thesis_invalid_if"] == "closes below the 50-day", d.targets
    assert m.soft_exit_retry_targets(_R({"targets": [1]})) == [1]
    assert m.soft_exit_retry_targets(None) == []
    assert "Symbols: XYZ" in m.soft_exit_fill_coda(["XYZ"])
    class _P: constructor_dropped = ["A"]
    p = _P(); m.add_constructor_dropped(p, ["A", "B"]); assert p.constructor_dropped == ["A", "B"]
    rows = []
    class _C: soft_exit_heals = {"XYZ": {"outcome": "failed"}}
    m.record_refusal_count(lambda *a, **k: rows.append((a, k)), _L(), None, _C(), ["xyz"])
    assert rows and rows[0][0][3] == m.REFUSAL_COUNT_STAGE and rows[0][1]["refused_count"] == 1, rows
    assert "failed" in m.soft_exit_heal_detail(_C(), "XYZ")
    for big in ("src.pipeline_stages", "src.stage_decision", "src.agents.portfolio_manager"):
        assert big not in sys.modules, big
    print("OK")
""")


def test_module_runs_from_stubs_without_the_owner_modules():
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parent.parent,
    )
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr
