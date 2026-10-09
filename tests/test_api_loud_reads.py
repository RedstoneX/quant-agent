"""Dashboard catch-alls log a full traceback; the endpoint answer is unchanged."""

import logging

from src.api import drift_state, loud_reads, routes_live


def _errors(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]


def test_swallowed_fault_logs_a_traceback_and_keeps_the_answer(monkeypatch, caplog):
    def _boom(*a, **k):
        raise RuntimeError("synthetic read failure")

    monkeypatch.setattr("src.drift_state.load_drift_state", _boom)
    with caplog.at_level(logging.ERROR):
        out = drift_state.deploy_drift_state()
    assert out == {"status": "unknown", "reason": "drift state read failed"}
    recs = _errors(caplog)
    assert len(recs) == 1 and recs[0].exc_info[0] is RuntimeError
    assert "drift_state.read" in recs[0].getMessage()


def test_clean_pass_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr("src.drift_state.load_drift_state", lambda path=None: {})
    with caplog.at_level(logging.ERROR):
        out = drift_state.deploy_drift_state()
    assert out["status"] == "unknown" and out["reason"] == "no drift check recorded"
    assert caplog.records == []


def test_unreached_site_logs_nothing(caplog):
    with caplog.at_level(logging.ERROR):
        pass  # no route called: the helper is only ever invoked from a handler
    assert caplog.records == []


def test_bound_handler_logs_the_given_exception(caplog):
    err = ValueError("kept")
    with caplog.at_level(logging.ERROR):
        loud_reads.record_dashboard_fault("somewhere", err)
    assert _errors(caplog)[0].exc_info[1] is err


def test_route_handler_is_loud_and_still_returns_its_empty_shape(monkeypatch, caplog):
    def _boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(routes_live, "get_risk_limits", _boom)
    with caplog.at_level(logging.ERROR):
        out = routes_live._compute_risk_limits()
    assert out == routes_live.RiskLimits()
    assert _errors(caplog)[0].exc_info[0] is RuntimeError
