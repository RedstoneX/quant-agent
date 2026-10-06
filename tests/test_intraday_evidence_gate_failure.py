"""The shared evidence gate must fail closed on the public intraday path."""

from unittest.mock import MagicMock, patch

import pytest

from src import evidence_gate
from src.evidence_gate import EvidenceGateEvaluationError
from src.scheduler import TradingScheduler
from tests.test_intraday_scan import _qualifying_move_pipeline


def _pipeline_reaching_the_gate():
    pipeline = _qualifying_move_pipeline()
    pipeline._is_trading_day = MagicMock(return_value=True)
    pipeline.broker.is_trading_day.return_value = True
    return pipeline


@patch("src.pipeline_intraday.compute_indicators")
def test_public_intraday_gate_crash_stops_before_pm_risk_and_execution(
    mock_compute_indicators,
):
    mock_compute_indicators.return_value = MagicMock()
    pipeline = _pipeline_reaching_the_gate()

    with patch.object(evidence_gate, "evaluate", side_effect=RuntimeError("boom")):
        with pytest.raises(EvidenceGateEvaluationError, match="boom"):
            pipeline.run_intra_check()

    pipeline.decision_stage.run.assert_not_called()
    pipeline.risk_stage.run.assert_not_called()
    pipeline.execution_stage.run.assert_not_called()


@patch("src.pipeline_intraday.compute_indicators")
@patch("src.scheduler.format_session_result", return_value="FAILED intra_check")
def test_live_scheduler_receives_gate_fault_and_notifies_owner(
    mock_format, mock_compute_indicators,
):
    mock_compute_indicators.return_value = MagicMock()
    pipeline = _pipeline_reaching_the_gate()
    config = MagicMock()
    config.notifications.mission_control_url = ""
    config.storage.db_path = None
    with patch("src.scheduler.TradingPipeline", return_value=pipeline):
        scheduler = TradingScheduler(config)
    scheduler.notifier = MagicMock()

    with patch.object(evidence_gate, "evaluate", side_effect=RuntimeError("boom")):
        scheduler._run_safe(pipeline.run_intra_check, "intra_check")

    error = mock_format.call_args.kwargs["error"]
    assert isinstance(error, EvidenceGateEvaluationError)
    assert "boom" in str(error)
    scheduler.notifier.send.assert_called_with(
        "FAILED intra_check", symbols=[], preserve_structural_markup=True,
        category="operational",
    )


def test_one_shot_main_receives_gate_fault_notifies_and_stays_nonzero(monkeypatch):
    """Production's timer entrypoint must see the fault, push FAILED, and raise."""
    import main as main_mod

    sent = []
    notifier = MagicMock()
    notifier.send.side_effect = lambda message, **_kwargs: sent.append(message) or True
    pipeline = MagicMock()
    pipeline.run_intra_check.side_effect = EvidenceGateEvaluationError(
        "evidence gate evaluation failed: boom"
    )

    monkeypatch.setattr(main_mod, "TelegramNotifier", lambda: notifier)
    monkeypatch.setattr(main_mod, "load_config", lambda _path: MagicMock())
    monkeypatch.setattr(main_mod, "refresh_pricing", lambda: None)
    monkeypatch.setattr(main_mod, "TradingPipeline", lambda _config: pipeline)
    monkeypatch.setattr("sys.argv", ["main.py", "--mode", "intra_check"])

    with pytest.raises(EvidenceGateEvaluationError, match="boom"):
        main_mod.main()

    assert any(
        "FAILED" in message and "evidence gate evaluation failed: boom" in message
        for message in sent
    )
