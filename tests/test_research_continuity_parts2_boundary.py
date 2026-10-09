"""Boundary witnesses, second research-continuity instalment: the carry-forward
readers, the insider memory, the Form 4 backlog record, the heal records and the
seat healer build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each class
is built from stubs alone (clause 5 of tests/boundary_harness.py). Storage and the
evidence journal are handed in. Nothing here names the pipeline class, imports its
module, or patches anything by module path.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.research_continuity.carry_forward import CarryForward, CarryForwardReaders
from src.research_continuity.form4_backlog import Form4BacklogRecorder
from src.research_continuity.heal_records import HealRecords
from src.research_continuity.insider_memory import InsiderMemory
from src.research_continuity.seat_heal_path import SeatHealer
from tests.boundary_harness import check_boundary
from tests.fake_event_journal import InMemoryEventJournal

PARTS = [Form4BacklogRecorder, CarryForwardReaders, InsiderMemory, HealRecords, SeatHealer]


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


@pytest.mark.parametrize("cls", PARTS)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize(
    "module",
    [
        "src.research_continuity.form4_backlog",
        "src.research_continuity.insider_memory",
        "src.research_continuity.heal_records",
        "src.research_continuity.seat_heal_path",
    ],
)
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_carry_forward_module_passes_the_boundary_check():
    verdict = check_boundary("src.research_continuity.carry_forward")
    # Clause 1 names exactly the frozen @dataclass `CarryForward`, whose
    # __init__ is generated; it moved verbatim. Nothing else may fail.
    assert set(verdict.failures) <= {1}, verdict.failures
    assert verdict.failures.get(1) == ["CarryForward: no __init__"], verdict.failures


# ── Form 4 backlog ──


def test_form4_backlog_is_written_through_the_journal_handed_in():
    journal = InMemoryEventJournal()
    rec = Form4BacklogRecorder(journal=journal)
    rec._record_form4_backlog("run-1", {"status": "ok", "pending_filings": 3, "ignored": 1})
    rec._record_congressional_refresh("run-1", {"congressional": {"new": 2}})
    rec._record_congressional_refresh("run-1", {"congressional": None})
    assert [(r["kind"], r["run_id"]) for r in journal.rows] == [
        ("form4_backlog", "run-1"),
        ("congressional_refresh", "run-1"),
    ]
    assert '"ignored"' not in journal.rows[0]["evidence_json"]


def test_form4_backlog_alert_pages_only_when_the_morning_read_did_not_finish(monkeypatch):
    import src.notifier as notifier
    from src.util.time import et_today

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    rec = Form4BacklogRecorder(journal=InMemoryEventJournal())
    rec._alert_form4_backlog_before_open(
        {
            "watched_read_through": et_today().isoformat(),
            "watched_pending_filings": 0,
            "watched_unchecked_names": [],
            "watched_drain_ran": True,
            "edgar_coverage": {"verified": True, "known": True},
        }
    )
    assert sent == []
    rec._alert_form4_backlog_before_open({"watched_read_through": "", "watched_pending_filings": 2})
    assert len(sent) == 1 and "2 company filing(s)" in sent[0]


# ── carry-forward readers ──


def _readers(**overrides):
    base = dict(
        db=None,
        macro_store=None,
        news_store=None,
        earnings_provider=None,
        macro_regime_or_print_changed=lambda payload: False,
        news_has_newer_material_wire=lambda report: False,
        peeked_news_wire_text=lambda: "",
    )
    base.update(overrides)
    return CarryForwardReaders(**base)


def test_macro_carry_forward_reads_the_store_and_the_detector_handed_in():
    from src.trading_calendar import et_today

    today = et_today().isoformat()
    store = SimpleNamespace(load_last_state=lambda: {"date": today, "regime": "risk-on"})
    carried = _readers(macro_store=store)._carry_forward_macro()
    assert isinstance(carried, CarryForward) and carried.same_session is True
    assert carried.payload["regime"] == "risk-on" and carried.status == "carried_from_morning"
    changed = _readers(macro_store=store, macro_regime_or_print_changed=lambda p: True)._carry_forward_macro()
    assert changed.payload is None and changed.status == "expired"

    def boom():
        raise RuntimeError("store down")

    failed = _readers(macro_store=SimpleNamespace(load_last_state=boom))._carry_forward_macro()
    assert (failed.payload, failed.status, failed.same_session) == (None, "carry_forward_failed", False)


def test_earnings_carry_forward_without_a_provider_is_not_run_intraday():
    carried = _readers()._carry_forward_earnings(SimpleNamespace(run_id="r", session="intra_check"))
    assert (carried.payload, carried.status) == ([], "not_run_intraday")


# ── insider memory ──


def _insider(**overrides):
    base = dict(db=None, smart_money_provider=None, watched_research_symbols=lambda ctx=None, report=None: [])
    base.update(overrides)
    return InsiderMemory(**base)


def test_insider_freshness_without_a_provider_is_unknown_not_quiet():
    verdict = _insider()._form4_freshness()
    assert verdict["ok"] is False and "cannot answer" in verdict["reason"]
    probe = SimpleNamespace(form4_freshness=lambda symbols: {"ok": True, "new_filings": ["0001"]})
    assert _insider(smart_money_provider=probe)._form4_freshness(symbols=["AAPL"])["new_filings"] == ["0001"]


def test_remembered_insider_findings_come_from_the_storage_handed_in():
    finding = json.dumps(
        {
            "symbol": "AAPL",
            "stance": "bullish",
            "economic_role": "actionable",
            "summary": "CEO bought",
            "why_now": "open-market buy this week",
            "observations": [
                {
                    "symbol": "AAPL",
                    "actor": "CEO",
                    "direction": "buy",
                    "transaction_date": "2026-09-30",
                    "disclosure_date": "2026-10-01",
                    "source_url": "https://www.sec.gov/x",
                    "lag_days": 1,
                    "disclosure_age_days": 1,
                    "freshness": "fresh",
                    "economic_role": "actionable",
                    "accession_number": "0001-24-000001",
                }
            ],
        }
    )
    rows = {"first": [("run-9",)], "second": [(finding,)]}
    calls: list[str] = []

    class _Cursor:
        def __init__(self, items):
            self._items = items

        def fetchone(self):
            return self._items[0] if self._items else None

        def fetchall(self):
            return self._items

    def execute(sql, params):
        calls.append(sql)
        return _Cursor(rows["first"] if "SELECT run_id" in sql else rows["second"])

    findings, accessions = _insider(db=SimpleNamespace(execute=execute))._load_remembered_insider_findings(ctx=None)
    assert [f.symbol for f in findings] == ["AAPL"]
    assert accessions == {"0001-24-000001"} and len(calls) == 2


def test_insider_carry_forward_expires_when_the_probe_cannot_call_it_current():
    provider = SimpleNamespace(form4_freshness=lambda symbols: {"ok": False, "reason": "partial"})
    carried = _insider(smart_money_provider=provider)._carry_forward_insider(SimpleNamespace(smart_money_findings=[]))
    assert carried.status == "expired" and carried.payload == []


# ── heal records + seat healer ──


class _Analyst:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    def analyze(self, summary, session=None):
        self.calls += 1
        if self.answer is None:
            raise RuntimeError("model down")
        return self.answer, SimpleNamespace(
            user_message="prompt",
            raw_text="{}",
            model="m",
            tokens_used=5,
            input_tokens=3,
            output_tokens=2,
            cost_usd=0.01,
        )


class _Ledger:
    def __init__(self):
        self.logs: list[dict] = []
        self.spent = 0

    def insert_agent_log(self, **kwargs):
        self.logs.append(kwargs)

    def count_paid_seat_heals_today(self, seat):
        return self.spent


def _ctx(**overrides):
    base = dict(
        run_id="run-7",
        session="intra_check",
        data_status={"macro": "carry_forward_failed"},
        heal_paid_retries={},
        macro_summary={"series": {"DGS10": 4.1}},
        macro_analysis=None,
        news_intel=None,
        heal_news_text="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _healer(*, db=None, macro_store=None, **overrides):
    ledger = db if db is not None else _Ledger()
    records = HealRecords(
        db=ledger, journal=InMemoryEventJournal(), macro_store=macro_store, macro=None, news_store=None
    )
    base = dict(
        db=ledger,
        record_heal=records._record_heal,
        persist_heal_call=records._persist_heal_call,
        persist_healed_macro_store=records._persist_healed_macro_store,
        cover_healed_news_wire=records._cover_healed_news_wire,
        require_paid_analysis=lambda name: None,
        agent_for=lambda name: None,
    )
    base.update(overrides)
    healer = SeatHealer(**base)
    healer.journal = records.journal  # test handle only; the part itself never holds a journal
    return healer


def test_one_paid_macro_retry_keeps_the_answer_the_price_and_the_store():
    saved: list[tuple] = []
    answer = SimpleNamespace(
        model_dump=lambda: {"regime": "risk-off"}, model_dump_json=lambda: '{"regime": "risk-off"}'
    )
    analyst = _Analyst(answer)
    healer = _healer(
        agent_for=lambda name: analyst if name == "macro_analyst" else None,
        macro_store=SimpleNamespace(save_last_state=lambda p, series_prints=None: saved.append((p, series_prints))),
    )
    ctx = _ctx()
    assert healer._try_one_paid_research_retry(ctx, "macro") is True
    assert analyst.calls == 1 and ctx.macro_analysis == {"regime": "risk-off"}
    assert ctx.data_status["macro"] == "ok" and ctx.heal_paid_retries == {"macro": 1}
    assert [r["kind"] for r in healer.journal.rows] == ["analysis", "seat_heal"]
    assert healer.db.logs[0]["agent_name"] == "macro_analyst" and healer.db.logs[0]["cost_usd"] == 0.01
    assert saved and saved[0][0] == {"regime": "risk-off"}


def test_the_day_cap_and_the_spend_gate_are_the_collaborators_handed_in():
    from src.cost_circuit import PaidAnalysisSuspended

    analyst = _Analyst(SimpleNamespace(model_dump=lambda: {}, model_dump_json=lambda: "{}"))
    ledger = _Ledger()
    ledger.spent = 1
    capped = _healer(db=ledger, agent_for=lambda name: analyst)
    assert capped._try_one_paid_research_retry(_ctx(), "macro") is False
    assert analyst.calls == 0 and [r["kind"] for r in capped.journal.rows] == ["seat_heal"]
    assert '"day_cap"' in capped.journal.rows[0]["evidence_json"]

    def suspended(name):
        raise PaidAnalysisSuspended("cap")

    blocked = _healer(agent_for=lambda name: analyst, require_paid_analysis=suspended)
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        assert blocked._try_one_paid_research_retry(_ctx(), "macro") is False
    assert analyst.calls == 0 and alert.call_count == 1
    assert '"cap_blocked"' in blocked.journal.rows[0]["evidence_json"]


def test_heal_dispatcher_records_a_lost_seat_it_could_not_refresh():
    healer = _healer()  # no analyst at all: nothing to pay, still a forensic row
    ctx = _ctx(data_status={"macro": "carry_forward_failed", "news": "carry_forward_empty"})
    healer._heal_lost_research_seats(ctx)
    rows = healer.journal.rows
    assert len(rows) == 1 and '"paid_retry_attempted": false' in rows[0]["evidence_json"]
    assert ctx.data_status == {"macro": "carry_forward_failed", "news": "carry_forward_empty"}


def test_a_failed_journal_never_undoes_a_heal_record():
    records = HealRecords(
        db=None, journal=InMemoryEventJournal(fail=True), macro_store=None, macro=None, news_store=None
    )
    from src.seat_heal import HEAL_FAILED, HealResult

    records._record_heal(_ctx(), HealResult(seat="macro", outcome=HEAL_FAILED, reason="x"), alert=False)
    assert len(records.journal.failures) == 1 and records.journal.rows == []


def test_a_host_override_of_a_lifted_body_is_honoured():
    seen: list = []
    healer = _healer(record_heal=lambda ctx, result, alert=False: seen.append(result.outcome))
    healer._heal_lost_research_seats(_ctx())
    assert seen == ["failed"]
