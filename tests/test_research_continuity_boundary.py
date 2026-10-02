"""Boundary test (docs/ARCHITECTURE.md section 3, clause 5): ResearchContinuity is
built and exercised with a fake journal and stand-in stores, no pipeline object."""
import ast
import datetime
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS

from src.pipeline_research_continuity import CarryForward, ResearchContinuity
from src.seat_heal import HealResult
from tests.fake_event_journal import InMemoryEventJournal

SRC = Path(__file__).resolve().parents[1] / "src" / "pipeline_research_continuity.py"


class _Store:
    def __init__(self, state=None, boom=False):
        self.state, self.boom = state, boom

    def load_last_state(self):
        if self.boom:
            raise RuntimeError("store down")
        return self.state


def _rc(journal=None, **kw):
    return ResearchContinuity(
        config=NS(news=NS(max_prompt_items=5)),
        journal=journal if journal is not None else InMemoryEventJournal(), **kw,
    )


CTX = NS(run_id="run-1", session="intra_check", macro_summary={})
CALL = NS(user_message="u", raw_text="r", model="m", tokens_used=7,
          input_tokens=3, output_tokens=4, cost_usd=0.5)


def test_form4_backlog_is_recorded_through_the_journal():
    j = InMemoryEventJournal()
    _rc(j)._record_form4_backlog("r1", {"edgar_coverage": 3, "ignored": 1})
    (row,) = j.events(kind="form4_backlog")
    assert (row["run_id"], row["agent_name"], row["scope"]) == ("r1", "smart_money_refresh", "run")
    body = json.loads(row["evidence_json"])
    assert body["edgar_coverage"] == 3 and "ignored" not in body


def test_congressional_refresh_skipped_without_summary():
    j = InMemoryEventJournal()
    _rc(j)._record_congressional_refresh("r1", {})
    assert j.rows == []


def test_paid_heal_writes_agent_log_and_analysis_evidence():
    j = InMemoryEventJournal()
    _rc(j)._persist_heal_call(CTX, "news", "news_analyst", {"k": "v"}, CALL)
    (log,) = j.agent_logs
    assert log["agent_name"] == "news_analyst_intra_check" and log["tokens_used"] == 7
    (ev,) = j.events(kind="analysis")
    assert ev["agent_name"] == "news_analyst" and json.loads(ev["evidence_json"]) == {"k": "v"}


def test_journal_failure_never_undoes_a_heal(caplog):
    with caplog.at_level(logging.WARNING):
        _rc(InMemoryEventJournal(fail=True))._persist_heal_call(
            CTX, "macro", "macro_analyst", {"k": 1}, CALL)
        _rc(InMemoryEventJournal(fail=True))._record_heal(
            CTX, HealResult(seat="news", outcome="failed", reason="x"), alert=False)
    assert "paid-call log write failed" in caplog.text


def test_macro_carry_forward_reads_the_stand_in_store():
    today = datetime.date.today().isoformat()
    cf = _rc(macro_store=_Store({"date": today, "regime": "risk_on"}))._carry_forward_macro()
    assert isinstance(cf, CarryForward) and cf.status == "carried_from_morning" and cf.same_session
    assert _rc(macro_store=_Store())._carry_forward_macro().status == "carry_forward_empty"
    assert _rc(macro_store=_Store(boom=True))._carry_forward_macro().status == "carry_forward_failed"


def test_wire_peek_state_is_owned_by_the_instance():
    class _Prov:
        def fetch_news(self, symbols=None):
            return ([{"title": "AAPL beats", "summary": "x"}], {})

    rc = _rc(news_provider=_Prov())
    rc._watched_research_symbols = lambda ctx=None, report=None: ["AAPL"]
    assert rc._peek_news_headlines(None) == ["AAPL beats"]
    assert rc._last_news_peek_items == [{"title": "AAPL beats", "summary": "x"}]


def test_clauses_1_to_3_by_ast():
    tree = ast.parse(SRC.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResearchContinuity")
    methods = {f.name for f in cls.body if isinstance(f, ast.FunctionDef)}
    init = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "__init__")
    assigned = {a.attr for n in ast.walk(init) for a in ast.walk(n)
                if isinstance(a, ast.Attribute) and isinstance(a.ctx, ast.Store)
                and isinstance(a.value, ast.Name) and a.value.id == "self"}
    reads = {a.attr for a in ast.walk(cls) if isinstance(a, ast.Attribute)
             and isinstance(a.value, ast.Name) and a.value.id == "self"}
    assert reads <= assigned | methods, sorted(reads - assigned - methods)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "src.pipeline" not in imported
    assert init.args.args[0].arg == "self" and len(init.args.args) == 1  # keyword-only
