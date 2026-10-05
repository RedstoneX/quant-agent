"""Board item 78, first box: a falsifier blanked by a later write is healed.

The seat's stated 'I'll sell if' sentence must survive a later blank
assignment on a live model. Three states stay distinguishable in the
durable `soft_exit_heal_restores` rows (via `drain_restore_observations`):
present (no row), healed (healed=True, source `prior_assigned_value`) and
genuinely absent (blank_found=True, healed=False, source None).
"""
import pytest

from src.models import TargetPosition
from src.seat_heal import drain_restore_observations as _drain


def drain_restore_observations():
    # Drop the validator's own `present` rows (blank_found=False) that fire
    # on every re-validation; they are not what these tests are about.
    obs, dropped = _drain()
    return [o for o in obs if o["blank_found"]], dropped

SENTENCE = "closes below the 50-day average on volume"


def _target(**kw):
    base = dict(symbol="NVDA", thesis="t", risk_allocation_pct=1.0)
    return TargetPosition(**{**base, **kw})


@pytest.mark.parametrize("blank", [None, "", "   ", "unknown"])
def test_later_blank_write_heals_back_the_stated_sentence(blank):
    t = _target(thesis_invalid_if=SENTENCE)
    drain_restore_observations()
    t.thesis_invalid_if = blank
    assert t.thesis_invalid_if == SENTENCE
    obs, _ = drain_restore_observations()
    assert obs == [{
        "symbol": "NVDA", "blank_found": True,
        "healed": True, "source": "prior_assigned_value", "occurrences": 1,
    }]


def test_blank_write_with_no_original_reads_gone_and_is_counted():
    t = _target(thesis_invalid_if="")
    drain_restore_observations()
    t.thesis_invalid_if = None
    assert t.thesis_invalid_if == "unknown"  # never filled with a sentence
    obs, _ = drain_restore_observations()
    assert obs == [{
        "symbol": "NVDA", "blank_found": True,
        "healed": False, "source": None, "occurrences": 1,
    }]


def test_a_new_stated_sentence_replaces_the_old_and_records_nothing():
    t = _target(thesis_invalid_if=SENTENCE)
    drain_restore_observations()
    t.thesis_invalid_if = "gaps down through the prior low"
    assert t.thesis_invalid_if == "gaps down through the prior low"
    assert not [o for o in drain_restore_observations()[0] if o["healed"]]
