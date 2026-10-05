"""Regression tests for the tech_analyst chunk-collapse replay defect.

`tech_analyst.analyze_batch` (src/agents/tech_analyst.py) can make several
real provider calls for one logical batch (one per 25-symbol chunk, plus one
missing-symbol recovery call), then stitches them into a SINGLE `agent_logs`
row before `pipeline_stages.py` logs it — the "N-chunks-collapse-to-1-row
limitation" its own comments name. `ops/rehearsal/replay.py` patches the
provider transport, which is invoked once per real call, so a merged row used
to starve replay after the first chunk: verified by running this harness
against the real production snapshot before the fix in this file existed —
the 2026-08-28 run-be9f8f06 tech_analyst row (a real, single agent_logs row
representing 4 real provider calls, confirmed by its
`provider_requests = 4` column) reproduced "all 1 recorded response(s) were
already replayed" on the second chunk, which cascaded into 6 provider
attempts and masked the actual incident (the Portfolio Manager cost-ceiling
trip) behind an unrelated `failed_call_unknown_cost` circuit trip.

These tests exercise the un-merge (`_unmerge_chunked_call`) and the matching
it feeds (`ResponseLibrary.match`) directly, with small synthetic rows built
in the exact shape `analyze_batch` produces — no database, no sudo, no
network; see test_rehearsal_reproduces_cost_ceiling.py for the full-pipeline
acceptance test against the real incident.
"""

from __future__ import annotations

import pytest

from ops.rehearsal.replay import (
    MissingRecordedResponse,
    RecordedCall,
    ResponseLibrary,
    _split_labelled_sections,
    _unmerge_chunked_call,
)


def _merged_call(
    *,
    row_id: int = 233,
    agent_name: str = "tech_analyst",
    run_id: str = "run-be9f8f06",
    parts: list[tuple[str, str, str]],  # (label, user_message, raw_response)
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None,
    finish_reason: str = "stop",
    actual_provider: str | None = "openrouter",
) -> RecordedCall:
    """Build one merged row exactly the way `analyze_batch` joins chunks:
    `"--- {label} ---\\n{content}"` items joined with `"\\n\\n"`."""
    input_message = "\n\n".join(f"--- {label} ---\n{msg}" for label, msg, _ in parts)
    full_response = "\n\n".join(f"--- {label} ---\n{resp}" for label, _, resp in parts)
    return RecordedCall(
        row_id=row_id, agent_name=agent_name, run_id=run_id,
        timestamp="2026-08-28 13:32:42", model="google/gemini-3.5-flash-lite",
        input_message=input_message, full_response=full_response,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, finish_reason=finish_reason,
        actual_provider=actual_provider,
    )


# ------------------------------------------------------------- un-merging


def test_unmerge_recovers_one_call_per_chunk():
    """A 3-chunk + 1-recovery merged row un-merges into 4 real calls, in order."""
    call = _merged_call(
        parts=[
            ("chunk 1/3", "AAPL bars here", '{"symbol": "AAPL"}'),
            ("chunk 2/3", "MSFT bars here", '{"symbol": "MSFT"}'),
            ("chunk 3/3", "GOOG bars here", '{"symbol": "GOOG"}'),
            ("missing-symbol recovery", "AAPL retry bars", '{"symbol": "AAPL"}'),
        ],
        input_tokens=1000, output_tokens=400, cost_usd=0.04,
    )
    parts = _unmerge_chunked_call(call)
    assert len(parts) == 4
    assert [p.part_label for p in parts] == [
        "chunk 1/3 (1/4)", "chunk 2/3 (2/4)", "chunk 3/3 (3/4)",
        "missing-symbol recovery (4/4)",
    ]
    assert parts[0].input_message == "AAPL bars here"
    assert parts[0].full_response == '{"symbol": "AAPL"}'
    assert parts[2].input_message == "GOOG bars here"
    # Every real row_id/run_id/agent_name/model/provider carries through —
    # they're constant across chunks of the same logical batch.
    for p in parts:
        assert p.row_id == 233
        assert p.agent_name == "tech_analyst"
        assert p.run_id == "run-be9f8f06"
        assert p.actual_provider == "openrouter"


def test_unmerge_preserves_exact_token_and_cost_totals():
    """Prorated per-chunk figures must sum back to exactly the recorded total —
    replay must not invent, inflate, or lose spend when it un-merges a row."""
    call = _merged_call(
        parts=[
            ("chunk 1/3", "x" * 1000, "y" * 200),
            ("chunk 2/3", "x" * 900, "y" * 180),
            ("chunk 3/3", "x" * 80, "y" * 30),
            ("missing-symbol recovery", "x" * 500, "y" * 90),
        ],
        input_tokens=326591, output_tokens=22612, cost_usd=0.0417039,
    )
    parts = _unmerge_chunked_call(call)
    assert sum(p.input_tokens for p in parts) == 326591
    assert sum(p.output_tokens for p in parts) == 22612
    assert round(sum(p.cost_usd for p in parts), 8) == 0.0417039
    # Every part actually got a share, not just the last one.
    assert all(p.input_tokens > 0 for p in parts)


def test_unmerge_cost_none_stays_none_on_every_part():
    """A row with no pinned cost (non-OpenRouter, or unknown model) must not
    have a cost invented for any of its recovered parts."""
    call = _merged_call(
        parts=[("chunk 1/2", "a", "b"), ("chunk 2/2", "c", "d")],
        input_tokens=100, output_tokens=50, cost_usd=None,
        actual_provider="anthropic",
    )
    parts = _unmerge_chunked_call(call)
    assert all(p.cost_usd is None for p in parts)


def test_unmerge_leaves_non_chunked_row_untouched():
    """A normal single-call agent (no chunk markers at all) is unaffected."""
    call = RecordedCall(
        row_id=230, agent_name="news_analyst_morning", run_id="run-be9f8f06",
        timestamp="2026-08-28 13:31:02", model="google/gemini-3.5-flash-lite",
        input_message="ordinary news prompt, no markers",
        full_response='{"headline": "..."}',
        input_tokens=500, output_tokens=100, cost_usd=0.002,
        finish_reason="stop", actual_provider="openrouter",
    )
    parts = _unmerge_chunked_call(call)
    assert parts == [call]
    assert parts[0].part_label is None


def test_unmerge_falls_back_when_markers_dont_match():
    """input_message and full_response must carry the SAME label sequence to
    be un-merged; a mismatch is an admission it can't be done reliably, not a
    guess — the row replays as one call exactly as it always has."""
    call = RecordedCall(
        row_id=999, agent_name="tech_analyst", run_id="run-x",
        timestamp="t", model="m",
        input_message="--- chunk 1/2 ---\na\n\n--- chunk 2/2 ---\nb",
        full_response="--- chunk 1/2 ---\nc",  # only one section on this side
        input_tokens=10, output_tokens=10, cost_usd=0.01,
        finish_reason="stop", actual_provider="openrouter",
    )
    parts = _unmerge_chunked_call(call)
    assert parts == [call]


def test_split_labelled_sections_returns_none_without_markers():
    assert _split_labelled_sections("just a normal prompt, no markers here") is None


# --------------------------------------------------------------- matching


def test_response_library_matches_each_chunk_independently():
    """The actual defect: replay must serve N distinct answers for N real
    chunk calls, not run out after the first."""
    call = _merged_call(
        parts=[
            ("chunk 1/2", "AAPL MSFT bars 40day rsi macd", '{"symbol": "AAPL", "rating": "buy"}'),
            ("chunk 2/2", "GOOG AMZN bars 40day rsi macd", '{"symbol": "GOOG", "rating": "sell"}'),
        ],
        input_tokens=2000, output_tokens=200, cost_usd=0.01,
    )
    library = ResponseLibrary([call], source_run_id="run-be9f8f06")
    assert library.available() == {"tech_analyst": 2}

    first = library.match("tech_analyst", "AAPL MSFT bars 40day rsi macd")
    assert first.full_response == '{"symbol": "AAPL", "rating": "buy"}'

    second = library.match("tech_analyst", "GOOG AMZN bars 40day rsi macd")
    assert second.full_response == '{"symbol": "GOOG", "rating": "sell"}'

    # A third live call for this agent has nothing left — and says so clearly,
    # naming the agent, rather than silently reusing an already-consumed part.
    with pytest.raises(MissingRecordedResponse) as excinfo:
        library.match("tech_analyst", "AAPL MSFT bars 40day rsi macd")
    assert "tech_analyst" in str(excinfo.value)
    assert library.findings[-1]["kind"] == "missing_recorded_response"
    assert library.findings[-1]["agent"] == "tech_analyst"


def test_response_library_reports_expanded_count_not_row_count():
    """Before the fix, `available()` reported the DB row count (1) even
    though 4 real calls were behind it — actively misleading about how much
    replay coverage actually exists. It must report the real call count."""
    call = _merged_call(
        parts=[
            ("chunk 1/3", "a", "1"),
            ("chunk 2/3", "b", "2"),
            ("chunk 3/3", "c", "3"),
            ("missing-symbol recovery", "d", "4"),
        ],
        input_tokens=400, output_tokens=40, cost_usd=0.02,
    )
    other = RecordedCall(
        row_id=230, agent_name="news_analyst_morning", run_id="run-be9f8f06",
        timestamp="t", model="m", input_message="news prompt",
        full_response="{}", input_tokens=10, output_tokens=5, cost_usd=0.001,
        finish_reason="stop", actual_provider="openrouter",
    )
    library = ResponseLibrary([call, other], source_run_id="run-be9f8f06")
    assert library.available() == {"tech_analyst": 4, "news_analyst": 1}


def test_response_library_still_matches_by_similarity_within_chunks():
    """Matching among the un-merged parts is still Jaccard similarity, not
    call order — consuming a merged row's parts in DB order would reintroduce
    the exact non-determinism this module's matching design exists to avoid
    (see replay.py's module docstring, "MATCHING")."""
    call = _merged_call(
        parts=[
            ("chunk 1/2", "alpha beta gamma delta epsilon", "resp-1"),
            ("chunk 2/2", "zeta eta theta iota kappa", "resp-2"),
        ],
        input_tokens=200, output_tokens=20, cost_usd=0.001,
    )
    library = ResponseLibrary([call])
    # Ask for chunk 2's content FIRST — matching must still find the right
    # answer rather than handing back chunk 1 because it happens to be first.
    chosen = library.match("tech_analyst", "zeta eta theta iota kappa extra words")
    assert chosen.full_response == "resp-2"
    chosen2 = library.match("tech_analyst", "alpha beta gamma delta epsilon")
    assert chosen2.full_response == "resp-1"


def test_match_does_not_crash_when_two_unmerged_parts_of_one_row_tie():
    """Regression: reproduced live against production history (2026-08-29) on
    a plain unpinned `morning` rehearsal, no incident pinning involved.

    `_unmerge_chunked_call` gives every part of one merged row the SAME
    row_id, so when a live prompt shares no words with either of two parts
    from the same original chunked call, both score 0.0 and (score, -row_id)
    ties completely between them. The old code put the `RecordedCall` itself
    in the sort tuple as a final tiebreaker
    (`sorted([(score, -row_id, call) ...], reverse=True)`), and `RecordedCall`
    defines no ordering, so Python raised
    `TypeError: '<' not supported between instances of 'RecordedCall' and
    'RecordedCall'` trying to break the tie. In production this cascaded:
    tech_analyst's retry logic caught it as a call failure, exhausted its
    primary-model attempts, failed over to a second provider, hit the exact
    same crash on the same tied candidates, burned through the cost circuit's
    provider-attempt limit, and suspended paid analysis for the rest of the
    session — a rig-only bug masquerading as a production incident.
    """
    call = _merged_call(
        parts=[
            ("chunk 1/2", "AAPL MSFT bars rsi macd", "resp-1"),
            ("chunk 2/2", "GOOG AMZN bars rsi macd", "resp-2"),
        ],
        input_tokens=200, output_tokens=20, cost_usd=0.001,
    )
    library = ResponseLibrary([call])
    # Shares zero words with either chunk -> both score 0.0 -> exact tie on
    # (score, -row_id) since both parts carry the same row_id.
    chosen = library.match("tech_analyst", "totally unrelated live prompt text")
    assert chosen.full_response in ("resp-1", "resp-2")
    # Deterministic: rerunning the identical scenario picks the same part.
    library2 = ResponseLibrary([call])
    chosen2 = library2.match("tech_analyst", "totally unrelated live prompt text")
    assert chosen2.full_response == chosen.full_response


# ---------------------------------------------------------------------------
# Choosing the recorded run: the 2026-09-02 "verdict was a coin flip" defect.
#
# Unpinned, a rehearsal drew each agent's answer independently from the whole
# recorded pool, so the verdict tracked how that shared pool happened to be
# consumed rather than the code under test — PASS and FAIL on identical code.
# `select_replay_run` is the default that closes it: one recorded run, chosen
# deterministically from (session, --as-of, database).
# ---------------------------------------------------------------------------


def _history_db(tmp_path, rows):
    """A minimal `agent_logs` carrying (run_id, agent_name, timestamp) rows."""
    import sqlite3

    path = tmp_path / "history.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE agent_logs (id INTEGER PRIMARY KEY, run_id TEXT, "
        "agent_name TEXT, timestamp TEXT, input_message TEXT, "
        "full_response TEXT)"
    )
    conn.executemany(
        "INSERT INTO agent_logs (run_id, agent_name, timestamp, "
        "input_message, full_response) VALUES (?, ?, ?, 'prompt', 'answer')",
        rows,
    )
    conn.commit()
    conn.close()
    return str(path)


def test_select_replay_run_pins_the_most_recent_complete_run(tmp_path):
    """The default has to be one run, and the newest usable one."""
    from ops.rehearsal.replay import select_replay_run

    db = _history_db(tmp_path, [
        ("run-old", "tech_analyst", "2026-08-30 13:30:00"),
        ("run-old", "portfolio_manager", "2026-08-30 13:34:00"),
        ("run-new", "tech_analyst", "2026-09-01 13:30:00"),
        ("run-new", "portfolio_manager", "2026-09-01 13:34:00"),
    ])
    choice = select_replay_run(db, "morning")
    assert choice.run_id == "run-new"
    assert choice.mode == "auto" and choice.complete
    # The reader must be told which run was compared, in the report itself.
    assert "run-new" in choice.reason


def test_select_replay_run_skips_a_run_that_never_reached_the_decision(tmp_path):
    """A morning that stopped in research cannot answer a decision-stage call;
    pinning to it would inject a MissingRecordedResponse production never had."""
    from ops.rehearsal.replay import select_replay_run

    db = _history_db(tmp_path, [
        ("run-whole", "tech_analyst", "2026-08-30 13:30:00"),
        ("run-whole", "portfolio_manager", "2026-08-30 13:34:00"),
        ("run-stub", "news_analyst_morning", "2026-09-01 13:30:00"),
        ("run-stub", "macro_analyst", "2026-09-01 13:31:00"),
    ])
    choice = select_replay_run(db, "morning")
    assert choice.run_id == "run-whole"


def test_select_replay_run_will_not_replay_the_future(tmp_path):
    """--as-of fixes the rehearsed instant; a run that had not started by then
    is not something that morning could have produced."""
    from ops.rehearsal.replay import select_replay_run

    db = _history_db(tmp_path, [
        ("run-before", "tech_analyst", "2026-09-01 13:30:00"),
        ("run-before", "portfolio_manager", "2026-09-01 13:34:00"),
        ("run-after", "tech_analyst", "2026-09-02 13:30:00"),
        ("run-after", "portfolio_manager", "2026-09-02 13:34:00"),
    ])
    choice = select_replay_run(
        db, "morning", not_after_utc="2026-09-01 13:35:00",
    )
    assert choice.run_id == "run-before"


def test_select_replay_run_never_pins_a_rehearsals_own_rows(tmp_path):
    """A rehearsal writes `rehearsal-<session>-<date>` rows into its sandbox.
    Replaying a replay would be self-referential nonsense."""
    from ops.rehearsal.replay import select_replay_run

    db = _history_db(tmp_path, [
        ("run-real", "tech_analyst", "2026-09-01 13:30:00"),
        ("run-real", "portfolio_manager", "2026-09-01 13:34:00"),
        ("rehearsal-morning-20260902", "tech_analyst", "2026-09-02 13:30:00"),
        ("rehearsal-morning-20260902", "portfolio_manager", "2026-09-02 13:34:00"),
    ])
    assert select_replay_run(db, "morning").run_id == "run-real"


def test_select_replay_run_says_so_when_it_cannot_pin_anything(tmp_path):
    """No pin is a rig limitation and has to read as one — silence here is
    how the unpinned default hid for as long as it did."""
    from ops.rehearsal.replay import select_replay_run

    db = _history_db(tmp_path, [
        ("evening-x", "evening_analyst", "2026-09-01 00:00:00"),
    ])
    choice = select_replay_run(db, "morning")
    assert choice.run_id is None
    assert choice.complete is False
    assert "NOT pinned" in choice.reason


def test_select_replay_run_is_deterministic(tmp_path):
    """Same inputs, same pin — the whole point of the change."""
    from ops.rehearsal.replay import select_replay_run

    rows = [
        ("run-a", "tech_analyst", "2026-09-01 13:30:00"),
        ("run-a", "portfolio_manager", "2026-09-01 13:34:00"),
        ("run-b", "tech_analyst", "2026-09-01 13:30:00"),
        ("run-b", "portfolio_manager", "2026-09-01 13:34:00"),
    ]
    db = _history_db(tmp_path, rows)
    picks = {select_replay_run(db, "morning").run_id for _ in range(5)}
    assert len(picks) == 1
