"""The shared evidence gate must fail closed on the intraday decision path."""

from unittest.mock import MagicMock, patch

import pytest

from src import evidence_gate
from src.pipeline_context import RunContext
from tests.test_intraday_scan import _qualifying_move_pipeline


@patch("src.pipeline_intraday.compute_indicators")
def test_intraday_gate_crash_stops_before_pm_risk_and_execution(
    mock_compute_indicators,
):
    mock_compute_indicators.return_value = MagicMock()
    pipeline = _qualifying_move_pipeline()
    context = RunContext.start("intra_check")

    with patch.object(evidence_gate, "evaluate", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            pipeline._run_intraday_opportunity_scan(context)

    pipeline.decision_stage.run.assert_not_called()
    pipeline.risk_stage.run.assert_not_called()
    pipeline.execution_stage.run.assert_not_called()
