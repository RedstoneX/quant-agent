"""Conversion step 6: the EventJournal port stands on its own.

This file deliberately never imports TradingPipeline (architecture section 3,
clause 5): the port and its SQLite adapter are built here with explicit
stand-ins and exercised directly.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from types import SimpleNamespace

import pytest

import src.ports.event_journal as port_module
from src.ports.event_journal import EventJournal
from src.storage.db import Database
from src.storage.event_journal import DatabaseEventJournal, pipeline_event_fields
from tests.fake_event_journal import InMemoryEventJournal

ROWS = "SELECT run_id, decision_id, agent_name, kind, scope, symbol, evidence_json FROM specialist_evidence ORDER BY id"


class _Recorder:
    def __init__(self, raise_: Exception | None = None):
        self.calls: list[dict] = []
        self.raise_ = raise_

    def insert_specialist_evidence(self, **kw):
        if self.raise_:
            raise self.raise_
        self.calls.append(kw)
        return len(self.calls)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "j.db"))
    d.initialize()
    yield d
    d.close()


def test_port_is_abstract_with_exactly_two_methods():
    with pytest.raises(TypeError):
        EventJournal()  # type: ignore[abstract]
    assert set(EventJournal.__abstractmethods__) == {
        "persist_evidence",
        "record_pipeline_event",
    }


def test_port_module_imports_no_adapter_or_service():
    tree = ast.parse(pathlib.Path(port_module.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    assert not {m for m in mods if m.startswith("src.")}, mods


def test_this_test_file_never_builds_a_pipeline():
    tree = ast.parse(pathlib.Path(__file__).read_text())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
    assert "src.pipeline" not in imported
    assert "Trading" + "Pipeline" not in imported


def test_sqlite_adapter_writes_rows_without_a_pipeline(db):
    journal = DatabaseEventJournal(db)
    journal.persist_evidence(
        run_id="r1",
        agent_name="tech_analyst",
        kind="tech",
        scope="symbol",
        evidence_json='{"b":1,"a":"ü"}',
        symbol="AAPL",
        decision_id="d1",
    )
    journal.record_pipeline_event(
        run_id="r2",
        decision_id="d2",
        symbol="MSFT",
        stage="risk",
        outcome="refused",
        reason="too_wide",
        width=3.5,
        nested={"y": None},
    )
    journal.record_pipeline_event(
        run_id="r2",
        decision_id=None,
        symbol=None,
        stage="execution",
        outcome="ok",
    )
    rows = [tuple(r) for r in db.conn.execute(ROWS).fetchall()]
    assert rows == [
        ("r1", "d1", "tech_analyst", "tech", "symbol", "AAPL", '{"b":1,"a":"ü"}'),
        (
            "r2",
            "d2",
            "pipeline",
            "pipeline_event",
            "symbol",
            "MSFT",
            '{"nested": {"y": null}, "outcome": "refused", "reason": "too_wide", "stage": "risk", "width": 3.5}',
        ),
        (
            "r2",
            None,
            "pipeline",
            "pipeline_event",
            "run",
            None,
            '{"outcome": "ok", "reason": "", "stage": "execution"}',
        ),
    ]


def test_sqlite_adapter_never_raises_on_storage_failure(caplog):
    journal = DatabaseEventJournal(_Recorder(raise_=RuntimeError("disk full")))
    journal.persist_evidence(run_id="x", agent_name="a", kind="k", scope="run", evidence_json="[]")
    journal.record_pipeline_event(run_id="x", decision_id=None, symbol="T", stage="s", outcome="o")
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert msgs == [
        "Failed to persist Stage 4 specialist evidence (agent=a kind=k scope=run symbol=None): disk full",
        "Failed to persist Stage 4 specialist evidence (agent=pipeline "
        "kind=pipeline_event scope=symbol symbol=T): disk full",
    ]


def test_legacy_pipeline_stages_shims_produce_identical_storage_calls():
    """The ~15 untouched call sites go through the shims; pin them to the port."""
    from src.pipeline_stages import _persist_evidence, _record_pipeline_event

    via_shim, via_port = _Recorder(), _Recorder()
    _persist_evidence(via_shim, run_id="r", agent_name="a", kind="k", scope="run", evidence_json="{}")
    _record_pipeline_event(
        SimpleNamespace(db=via_shim), SimpleNamespace(run_id="r2", decision_id="d"), "Q", "st", "out", "why", b="2", a=1
    )
    DatabaseEventJournal(via_port).persist_evidence(
        run_id="r", agent_name="a", kind="k", scope="run", evidence_json="{}"
    )
    DatabaseEventJournal(via_port).record_pipeline_event(
        run_id="r2", decision_id="d", symbol="Q", stage="st", outcome="out", reason="why", b="2", a=1
    )
    assert via_shim.calls == via_port.calls
    assert via_shim.calls[1]["evidence_json"] == '{"a": 1, "b": "2", "outcome": "out", "reason": "why", "stage": "st"}'


def test_in_memory_fake_matches_adapter_rows():
    fake, rec = InMemoryEventJournal(), _Recorder()
    for j in (fake, DatabaseEventJournal(rec)):
        j.record_pipeline_event(run_id="r", decision_id="d", symbol="Z", stage="s", outcome="o", reason="", k=1)
        j.persist_evidence(run_id="r", agent_name="macro", kind="m", scope="run", evidence_json="[]")
    assert fake.rows == rec.calls
    assert [r["kind"] for r in fake.events(kind="pipeline_event")] == ["pipeline_event"]
    failing = InMemoryEventJournal(fail=True)
    failing.persist_evidence(run_id="r", agent_name="a", kind="k", scope="run", evidence_json="[]")
    assert failing.rows == [] and len(failing.failures) == 1


def test_fields_helper_is_the_single_source_of_payload_shape():
    f = pipeline_event_fields(
        run_id="r", decision_id=None, symbol="", stage="s", outcome="o", reason="", details={"k": 1}
    )
    assert f["scope"] == "run" and f["symbol"] == ""
    assert (
        inspect.signature(EventJournal.record_pipeline_event).parameters.keys()
        == inspect.signature(DatabaseEventJournal.record_pipeline_event).parameters.keys()
    )
