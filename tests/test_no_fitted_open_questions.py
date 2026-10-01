"""An open question that can only be answered by FITTING is not a debt, it is a trap.

`docs/OUTCOME.md` bars deriving any trade-governing number from this desk's
own trade record: the desk has a few dozen closed positions, so any constant
tuned to them is fitted to noise and will not survive contact with the next
tape. The ledger's `open_question` field says what WOULD settle a number. If
that field names a study over the desk's own fills, outcomes or maximum
adverse excursion, then the row promises a resolution doctrine forbids, and
the next pass either performs the barred study or spends its budget
discovering, again, that it may not.

This has already happened twice. `min_stop_atr_multiple` carried a
maximum-adverse-excursion question that board item 90 deleted on 2026-09-30
after finding it was both barred AND the origin of the value it was supposed
to justify. On 2026-10-01 board item 185 found the identical wording still
sitting on the regime scalers that multiply into the same stop. Nothing
stopped the second copy, which is why this check exists rather than another
note asking people to remember.

Scope: the wording, not the truth. A question can pass this file and still be
the wrong question. See `tests/test_number_sources.py` for the gate that every
number is in the ledger at all; this file only polices what the ledger's own
open questions promise.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

LEDGER = Path(__file__).resolve().parents[1] / "config" / "number_ledger.yaml"

# Each pattern names a resolution that is a study over THIS DESK'S OWN trading
# record. Instrument reads (daily bars, quotes, the broker's own fee schedule,
# published methodology) are deliberately not matched: those are the sanctioned
# answers, and the desk's own bars are evidence about the market, not about the
# desk.
_FITTED_RESOLUTIONS = (
    r"(maximum )?adverse excursion",
    r"\bMAE\b",
    r"\bbacktest(ing|ed)?\b",
    r"curve[- ]fit",
    r"(this |the )desk'?s own (trades|fills|trade record|outcomes|P&L|pnl)",
    r"our own (trades|fills|outcomes)",
)

# A row may MENTION a barred study in order to record that it was rejected.
# The exemption is explicit and narrow: the text has to say so.
_REJECTION_MARKERS = (
    "bars",
    "barred",
    "forbid",
    "rejected",
    "replaced",
    "never a",
    "not a maximum",
    "doctrine",
)


def _rows() -> list[dict]:
    data = yaml.safe_load(LEDGER.read_text())
    return list(data["numbers"])


def _offending(text: str) -> str | None:
    for pattern in _FITTED_RESOLUTIONS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and not any(m in text.lower() for m in _REJECTION_MARKERS):
            return match.group(0)
    return None


def test_no_ledger_open_question_promises_a_fitted_resolution() -> None:
    """THE CHECK. No `open_question` may say it is settled by a study over
    this desk's own trading record.

    If this fails, rewrite the question as something readable off the
    instrument, off a published source, or off a RECORDING the desk does not
    yet make — and if nothing can settle it, say that and name the blocker.
    Do not re-pick the number.
    """
    bad = []
    for row in _rows():
        question = str(row.get("open_question") or "")
        hit = _offending(question)
        if hit:
            bad.append(f"{row['id']}: promises {hit!r}")
    assert not bad, (
        "ledger open questions that can only be answered by fitting to this "
        "desk's own trade record (docs/OUTCOME.md bars this):\n  "
        + "\n  ".join(bad)
    )


@pytest.mark.parametrize(
    "question",
    [
        "What does this desk's own maximum adverse excursion say about it?",
        "Settle it by backtesting the last forty trades.",
        "Measured on our own fills once there are enough of them.",
    ],
)
def test_the_check_actually_fires(question: str) -> None:
    """A gate nobody has seen fail is indistinguishable from no gate."""
    assert _offending(question) is not None


@pytest.mark.parametrize(
    "question",
    [
        "How far does ATR(14) understate the daily range after a regime turn?",
        "Which published index methodology states an absolute volatility bound?",
        "Replaced 2026-10-01: the old adverse-excursion wording is barred.",
    ],
)
def test_the_check_leaves_legitimate_questions_alone(question: str) -> None:
    assert _offending(question) is None


def test_the_three_regime_scalers_are_the_rows_this_came_from() -> None:
    """Regression pin for board item 185. The risk-off scaler multiplies into
    every stop AND into `widest_reachable_stop_atr_multiple`, so its open
    question is the one most likely to be answered by someone reaching for the
    desk's own trade history."""
    ids = {
        f"src.portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[{i}][1]"
        for i in (0, 1, 2)
    }
    seen = {row["id"] for row in _rows()} & ids
    assert seen == ids, f"regime scaler rows missing from the ledger: {ids - seen}"
    for row in _rows():
        if row["id"] in ids:
            assert _offending(str(row.get("open_question") or "")) is None
