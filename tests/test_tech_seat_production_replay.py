"""Item 157: replay REAL stored tech-seat answers through both the OLD
bare-array parse (`parse_json_rows()`, no `list_field`) and the NEW
wrapper-aware parse (`parse_json_rows(list_field="results")`), and assert
they produce byte-identical rows and malformed lists.

Why this matters more than a hand-written fixture can show: every one of
these answers predates the wrapper schema and is a bare JSON array (the old
prompt only ever asked for one), so this is the closest thing to a
guarantee that item 157's schema change does not silently change what the
desk already parses correctly today. A hand-written fixture only proves the
parser handles the shapes an engineer thought to write; this proves it
against shapes the model actually produced.

`tests/fixtures/tech_seat_production_answers_sample.json` is a bounded,
evenly-spaced sample of real `agent_logs.full_response` rows for
`agent_name='tech_analyst'`, read from the read-only production snapshot
available on this box (`/tmp/qamc_ro.db`, generated 2026-09-18 19:24 UTC).
That snapshot's `agent_logs` table holds 116 tech-seat calls spanning
2026-08-17 13:32:41 to 2026-09-18 19:16:18, which split (on the desk's own
`--- chunk N/M ---` markers) into 243 individual raw answers. ALL 243 were
replayed through both parsers when this fixture was built: zero
differences — identical rows, identical malformed lists, on every one.
This file keeps 7 of the 243 (evenly spaced across the full range, not
cherry-picked) as a standing regression fixture so this guarantee is
checked on every CI run, not just once by hand.

A reviewer separately reported 292 production answers (2026-08-17 to
2026-09-22) replayed against both parsers with zero differences — NOT
independently confirmed from this box, and possibly conflated with a
DIFFERENT, pre-existing measurement of the same count over the same start
date in docs/INCIDENT_HISTORY.md's 2026-09-19 entry (item #538's row-salvage
validation, which is a different code path and found 9 differences /
45 recoveries, not zero). This file's own 243/7 numbers are independently
verifiable from `/tmp/qamc_ro.db` by anyone who can read it; the 292 figure
is not re-asserted as fact here.
"""
import json
import re
from pathlib import Path

import pytest

from src.agents.base import AgentResult

# Same fence pattern `AgentResult.parse_json` itself uses to recover a
# fenced JSON block — reused here only to inspect the fixture's shape, not
# to duplicate the parser under test.
_FENCED_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tech_seat_production_answers_sample.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text())
_SAMPLES = _FIXTURE["samples"]


def test_fixture_is_not_empty_and_is_really_bare_arrays():
    """Sanity on the fixture itself: if this ever loads zero samples (a
    bad path, a schema change to the JSON), the parametrized test below
    would silently pass on nothing and this whole file would stop proving
    anything. Also confirms the premise stated in the module docstring —
    these are pre-wrapper bare arrays, not `{"results": [...]}` — so the
    round-trip below is actually exercising the NEW code path against OLD
    data, not comparing a wrapper to itself.
    """
    assert len(_SAMPLES) >= 5
    for sample in _SAMPLES:
        text = sample["raw_text"]
        # The desk's own answer can self-correct mid-response (a second
        # fenced block after the first, or none at all — some real answers
        # are unfenced); when fenced, the LAST block is what the parser
        # actually takes (see `parse_json`'s "latest correction" rule).
        # Not every real answer is well-formed JSON at this point — some of
        # these are exactly the "one garbled row" shape the row-by-row
        # salvage exists for (see tests/test_tech_row_salvage.py) — so this
        # only checks the outer shape is array-like, not that the whole
        # thing parses.
        matches = _FENCED_RE.findall(text)
        body = matches[-1] if matches else text
        inner = body.lstrip()
        assert inner.startswith("["), (
            f"fixture sample {sample.get('agent_log_id')} does not open "
            f"with a bare array — the module docstring's premise is wrong "
            f"for this one"
        )
        assert '"results"' not in inner.split("\n", 1)[0], (
            f"fixture sample {sample.get('agent_log_id')} already looks "
            f"like the NEW wrapper shape — it does not test what this file "
            f"claims to test"
        )


@pytest.mark.parametrize(
    "sample",
    _SAMPLES,
    ids=[f"{s['agent_log_id']}@{s['timestamp']}" for s in _SAMPLES],
)
def test_old_and_new_parse_identically_for_a_real_stored_answer(sample):
    text = sample["raw_text"]
    result_for_old = AgentResult(raw_text=text, tokens_used=0, model="m")
    result_for_new = AgentResult(raw_text=text, tokens_used=0, model="m")

    old = result_for_old.parse_json_rows(key_field="symbol")
    new = result_for_new.parse_json_rows(key_field="symbol", list_field="results")

    assert old is not None, "old parser found nothing in a real stored answer"
    assert new is not None, "new parser found nothing in a real stored answer"
    assert new.rows == old.rows, (
        f"wrapper-aware parse produced DIFFERENT rows than the pre-157 "
        f"parse for real stored answer {sample.get('agent_log_id')} — "
        f"the schema change altered production-observed behaviour"
    )
    assert new.malformed == old.malformed
