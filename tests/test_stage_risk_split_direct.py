"""Direct witness for src.stage_risk_helpers (never via the pipeline)."""
from types import SimpleNamespace

import src.stage_risk as stage_risk
import src.stage_risk_helpers as helpers


def _v(rule, message="m"):
    return SimpleNamespace(rule=rule, message=message)


def test_sector_alert_partial_then_degraded_and_never_downgraded():
    status = {}
    helpers._apply_sector_unresolved_alert(status, [_v("sector_unresolved_x")])
    assert status["sector"] == "partial"
    helpers._apply_sector_unresolved_alert(
        status, [_v("sector_unresolved_lookup_failed")])
    assert status["sector"] == "degraded"
    helpers._apply_sector_unresolved_alert(status, [_v("sector_unresolved_x")])
    assert status["sector"] == "degraded"


def test_no_alert_leaves_status_untouched():
    status = {}
    helpers._apply_sector_unresolved_alert(status, [_v("other")])
    assert status == {}


def test_stage_risk_reexports_the_same_objects():
    for name in ("ANALYSIS_DROP_KIND", "UNIDENTIFIED_DROP_KEY",
                 "_apply_sector_unresolved_alert", "_parse_loss_advisories",
                 "_persist_dropped_reasons", "_reconcile_parse_loss",
                 "_record_queued_earnings_refusals"):
        assert getattr(stage_risk, name) is getattr(helpers, name)
