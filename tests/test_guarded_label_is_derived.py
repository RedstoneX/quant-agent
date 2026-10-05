"""A guarded row's label names the module that recorded it, never a shared broker prefix."""
import ast
import pathlib

import pytest

from src.sentinel.guarded import origin_area

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("module,area", [
    ("src.execution.broker_parts.order_desk", "execution.broker_parts"),
    ("src.trader_feed.naked", "trader_feed"),
    ("src.pipeline_sizing", "pipeline_sizing"),
])
def test_area_is_read_off_the_recording_module(module, area):
    assert origin_area(module) == area


def test_no_site_writes_a_broker_prefix_by_hand():
    offenders = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "record_guarded_pass":
                arg = node.args[1] if len(node.args) > 1 else None
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("broker."):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
