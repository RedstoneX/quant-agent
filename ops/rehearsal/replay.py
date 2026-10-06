"""Recorded-response replay for a rehearsed session.

`agent_logs` keeps the exact `input_message` and `full_response` of every
model call this system has ever made. A rehearsal reuses them, so a rehearsed
morning re-plays that morning's actual model output: zero provider calls, zero
cost, and the same answer every time you run it.

WHERE THE PATCH GOES, AND WHY NOT `_execute`
--------------------------------------------
`src/replay.py` documents `BaseAgent._execute` as "the `_execute` seam", and
for its purposes — re-running one stored input through a changed prompt — that
is the right seam. It is the WRONG seam for a session rehearsal, and the
2026-08-28 incident is exactly why.

That morning the Portfolio Manager never reached a provider. It was stopped
inside `_execute`, by `cost_circuit.begin_call`, because the assembled prompt's
pre-call estimate projected session spend past the reserved-exposure ceiling.
Everything that matters about the failure — prompt assembly, the byte-based
token estimate, the reservation, the ceiling comparison — happens *inside*
`_execute` and *before* any provider is touched. A harness that replaced
`_execute` would hand back a recorded response and sail straight past the bug
it exists to catch.

So the patch goes one layer deeper, at the three provider transports that
`_execute` calls: `_anthropic_call`, `_call_openai`, `_call_deepseek`. Those
three are the complete set of methods that put bytes on the wire (`_call_
anthropic` delegates to `_anthropic_call`, and so does the cross-provider
failover path, so patching `_anthropic_call` covers both). Everything above
them runs untouched: the reservation, the per-attempt authorization, the retry
and failover loop, truncation detection, cost accounting, `complete_call`, and
the `agent_logs` write. The replayed call even calls `authorize(model)` at the
same point the real transport does, so the mid-flight re-authorization check
fires exactly when it would in production.

Nothing in `src/` changes. This is a monkeypatch owned entirely by the harness.

MATCHING
--------
Matching is by agent name and run, as specified — but that pair is not unique.
The morning research stage fans five analysts out across a thread pool, and
`tech_analyst` alone made four calls in the 2026-08-28 morning. Worse, thread
completion order is not stable, so consuming recordings in call order would
make the harness non-deterministic on exactly the stage most likely to differ.

So within the (agent, run) candidate pool, a call is matched to the recording
whose stored `input_message` is most similar to the prompt the live pipeline
just assembled — Jaccard overlap on word sets, which is cheap on the ~380KB
prompts this system produces and strongly discriminative (symbols, prices and
dates differ between candidates). Each recording is consumed once. Ties break
on the lowest `agent_logs` row id. Same input, same match, every time.

The match score is kept and reported. A low score is a real signal: it means
the prompt the pipeline assembled today no longer resembles the prompt that
produced the recorded answer, so the replayed answer is being applied to a
question it was not asked. That is a finding, not a silent success.

MISSING RECORDINGS
------------------
When no recording exists for a call, the harness does NOT invent one. It
raises, the pipeline's real failure path handles it, and the report says which
agent had no recorded response. The exception carries `status_code = 400` so
`_is_retryable` fast-fails it: retrying a missing recording cannot succeed, and
letting it burn the retry budget would distort the very cost accounting the
rehearsal is measuring.

CHUNKED AGENTS: THE N-CALLS-TO-1-ROW PROBLEM
---------------------------------------------
`tech_analyst.analyze_batch` (src/agents/tech_analyst.py) auto-chunks a large
symbol batch into several real provider calls, then stitches their
`AgentResult`s into ONE merged result before `pipeline_stages.py` logs it —
the "N-chunks-collapse-to-1-row limitation" its own comments name (Stage 0
audit F-3). `run-be9f8f06`, the 2026-08-28 morning run this harness exists to
reproduce, made 4 real tech_analyst provider calls (3 primary chunks + 1
missing-symbol recovery; `agent_logs.provider_requests = 4` on that row) but
logged exactly 1 `agent_logs` row.

Replay patches the transport, which is called once per real call, so it needs
4 replayable answers for that row and — before the fix below — found 1: the
first live chunk consumed it, and every chunk after raised
`MissingRecordedResponse`. Verified by running this harness against the real
production snapshot before this fix existed: tech_analyst's second chunk
failed with "all 1 recorded response(s) were already replayed", which
cascaded into 6 provider attempts and a `failed_call_unknown_cost` trip — a
different, unrelated failure mode that masked the PM cost-ceiling failure
this harness is for.

The fix does not need new recordings. `analyze_batch` joins each real call's
`user_message` / `raw_text` behind a `"--- chunk i/N ---"` or
`"--- missing-symbol recovery ---"` marker line (and `_merge_agent_results`
nests a `"--- retry ---"` marker the same way for a chunk-internal retry), in
call order, in BOTH `input_message` and `full_response`. That marker sequence
is a complete, ordered record of the real calls a merged row represents, so
`_unmerge_chunked_call` below un-merges one `agent_logs` row back into one
`RecordedCall` per real call before it ever reaches `.match()`. A row with no
markers (every non-chunked agent, and any tech_analyst row recorded before
chunking existed) is returned unchanged. `input_tokens` / `output_tokens` /
`cost_usd` — only known merged, never per-chunk, in the database — are
prorated by each part's share of the row's total text length, with the last
part taking the remainder so the parts' sum is always exactly the recorded
total: replay must not invent spend, over- or under-count it, only place it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from ops.rehearsal.recorded_calls import (
    _split_labelled_sections, _unmerge_chunked_call, expand_and_audit,
)
from ops.rehearsal.response_matching import (
    MissingRecordedResponse, WORD, match_recorded_call, normalise,
)

logger = logging.getLogger(__name__)

# `RunContext.start` (src/pipeline_context.py) mints run ids as
# "<prefix>-<8 hex>", with the morning session keeping the legacy bare "run"
# prefix and every other session using its own name. Auto-pinning has to
# reverse that mapping to find the recorded runs of one session type.
SESSION_RUN_PREFIX = {
    "morning": "run",
    "midday": "midday",
    "close": "close",
    "evening": "evening",
    "intra_check": "intra_check",
}

# The agents a recorded run must contain before it counts as a COMPLETE
# example of that session — the deepest stage the session reaches, plus what
# that stage depends on. Derived from what live history actually contains
# (agent_logs, 2026-09-02: 32 recorded morning runs, 26 of which reached the
# portfolio manager; the other 6 stopped in research and cannot answer a
# rehearsal's decision-stage calls at all).
#
# Requiring MORE than a session strictly needs is the safe direction: spare
# recordings go unused, missing ones raise MissingRecordedResponse and inject
# a failure production never had. `risk_manager` is deliberately NOT required
# for morning — a portfolio manager that proposes nothing legitimately never
# calls it, and 21 of the 32 recorded mornings end that way.
SESSION_REQUIRED_AGENTS = {
    "morning": ("tech_analyst", "portfolio_manager"),
    "midday": ("news_analyst", "position_reviewer"),
    "close": ("news_analyst", "position_reviewer"),
    "evening": ("evening_analyst",),
    "intra_check": ("tech_analyst", "portfolio_manager"),
}

# `--replay-run` values that are instructions rather than run ids.
REPLAY_RUN_AUTO = "auto"
REPLAY_RUN_ANY = "any"


@dataclass
class RecordedCall:
    """One historical provider call, exactly as production logged it."""

    row_id: int
    agent_name: str
    run_id: str
    timestamp: str
    model: str
    input_message: str
    full_response: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    finish_reason: str | None
    actual_provider: str | None
    # Number of real transport attempts represented by the source
    # `agent_logs` row.  A value greater than the number of recoverable
    # chunk/retry sections means the row retained only the final answer and
    # cannot faithfully replay the failures that preceded it.
    provider_requests: int = 1
    consumed: bool = False
    # Set only when this call was recovered from a chunked agent's merged
    # row (see `_unmerge_chunked_call`) — names which real call this is
    # ("chunk 2/3 (2/4)") so a report or a MissingRecordedResponse can say
    # precisely which one, instead of just repeating the shared row_id.
    part_label: str | None = None
    _words: frozenset[str] | None = field(default=None, repr=False, compare=False)

    @property
    def words(self) -> frozenset[str]:
        if self._words is None:
            self._words = frozenset(WORD.findall(self.input_message or ""))
        return self._words

    def reported_cost(self) -> float | None:
        """What to feed back as the provider's own billed figure.

        Only OpenRouter reports a per-call charge; every other provider leaves
        this None and `_execute` prices the call from the pinned rate table.
        Replaying the recorded figure for OpenRouter reproduces production's
        accounting exactly; replaying it for anyone else would invent a
        provider report that never existed.
        """
        provider = (self.actual_provider or "").lower()
        if "openrouter" in provider and self.cost_usd is not None:
            return float(self.cost_usd)
        return None


# ------------------------------------------------------- choosing the run
#
# WHY A REHEARSAL PINS ITS REPLAY BY DEFAULT
# ------------------------------------------
# Unpinned, `.match()` draws from every recorded response this system has ever
# produced, and each recording is consumed once. That makes the pool a shared,
# order-sensitive resource: any change to how many calls the pipeline makes —
# or to how many analyses parse, which changes the prompts, which changes the
# Jaccard ranking — consumes it differently, so a DIFFERENT historical answer
# gets replayed. The verdict then tracks pool consumption rather than the code
# under test.
#
# Measured on 2026-09-02 (docs/INCIDENT_HISTORY.md, "the rehearsal rig's
# verdict was a coin flip"): a fix that eliminated 10 parse failures and
# recovered 2 symbols — strictly an improvement — flipped an unpinned morning
# rehearsal from PASS to FAIL. Pinned to one run, both commits FAIL
# identically and the improvement shows up where it should, as rejections
# falling from 23 to 21.
#
# The symptom was `pm_grounding_error` naming `ZS`: the unpinned matcher had
# handed the morning's decision stage a portfolio-manager answer recorded in
# `intra_check-d0909ddc` (agent_logs row 312) instead of the morning's own row
# 309, and the grounding check then judged a decision about `ZS` against a
# session that never analysed `ZS`.
#
# So the default is now "the most recent COMPLETE recorded run of the session
# being rehearsed, that had already started by the rehearsed instant", which
# makes the verdict a function of (code, session, --as-of, database) and
# nothing else. `--replay-run <id>` still pins explicitly; `--replay-run any`
# asks for the old pool-wide behaviour on purpose.


@dataclass
class ReplayRunChoice:
    """Which recorded run a rehearsal replays, and how that was decided."""

    run_id: str | None
    mode: str          # "explicit" | "auto" | "any"
    reason: str        # one plain sentence, printed in the report
    complete: bool = True
    candidates_considered: int = 0

    def as_note(self) -> str:
        return self.reason


def select_replay_run(
    db_path: str, session: str, *, not_after_utc: str | None = None,
) -> ReplayRunChoice:
    """Pick the recorded run an unpinned rehearsal of `session` should replay.

    Deterministic by construction: candidate runs are filtered on session
    prefix, on having started at or before `not_after_utc` (the rehearsed
    instant, as a UTC 'YYYY-MM-DD HH:MM:SS' string — `agent_logs.timestamp`
    defaults to SQLite's `datetime('now')`, which is UTC), and on carrying a
    recording for every agent in `SESSION_REQUIRED_AGENTS`. The survivors are
    ordered by (start timestamp, run id) and the last one wins.

    Falls back, loudly, rather than silently: an incomplete run is preferred
    to no pin at all, and no pin at all is reported as a rig limitation.
    """
    prefix = SESSION_RUN_PREFIX.get(session)
    if not prefix:
        return ReplayRunChoice(
            run_id=None, mode="any", complete=False,
            reason=(
                f"replay was NOT pinned: '{session}' has no known run-id "
                f"prefix, so recorded responses are drawn from all history "
                f"and this verdict is not reproducible"
            ),
        )

    query = (
        "SELECT run_id, agent_name, MIN(timestamp) AS started "
        "FROM agent_logs "
        "WHERE input_message IS NOT NULL AND input_message != '' "
        "AND full_response IS NOT NULL AND run_id LIKE ? "
        "GROUP BY run_id, agent_name"
    )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, (f"{prefix}-%",)).fetchall()
    finally:
        conn.close()

    starts: dict[str, str] = {}
    agents: dict[str, set[str]] = {}
    for row in rows:
        run = str(row["run_id"])
        # A rehearsal writes its own rows into the sandbox under
        # "rehearsal-<session>-<date>"; those are never replay material.
        if run.startswith("rehearsal-"):
            continue
        started = str(row["started"] or "")
        starts[run] = min(starts[run], started) if run in starts else started
        agents.setdefault(run, set()).add(normalise(str(row["agent_name"])))

    if not_after_utc:
        starts = {r: t for r, t in starts.items() if t and t <= not_after_utc}

    required = set(SESSION_REQUIRED_AGENTS.get(session, ()))
    ordered = sorted(starts, key=lambda r: (starts[r], r))
    complete = [r for r in ordered if required.issubset(agents.get(r, set()))]

    if complete:
        chosen = complete[-1]
        return ReplayRunChoice(
            run_id=chosen, mode="auto", complete=True,
            candidates_considered=len(ordered),
            reason=(
                f"replay pinned automatically to {chosen}, the most recent "
                f"complete {session} run recorded at or before the rehearsed "
                f"instant (started {starts[chosen]} UTC; {len(complete)} of "
                f"{len(ordered)} recorded {session} runs were complete). Pass "
                f"--replay-run to override"
            ),
        )
    if ordered:
        chosen = ordered[-1]
        missing = sorted(required - agents.get(chosen, set()))
        return ReplayRunChoice(
            run_id=chosen, mode="auto", complete=False,
            candidates_considered=len(ordered),
            reason=(
                f"replay pinned automatically to {chosen}, but NO recorded "
                f"{session} run is complete — this one has no recording for "
                f"{', '.join(missing)}, so calls to it cannot be answered and "
                f"this rehearsal exercises less than a whole session"
            ),
        )
    return ReplayRunChoice(
        run_id=None, mode="any", complete=False,
        reason=(
            f"replay was NOT pinned: no recorded {session} run exists at or "
            f"before the rehearsed instant, so responses are drawn from all "
            f"history and this verdict is NOT reproducible — treat it as "
            f"unmeasured, not as a result"
        ),
    )


class ResponseLibrary:
    """Recorded model responses for one run, matched to live calls."""

    def __init__(self, calls: list[RecordedCall], *, source_run_id: str | None = None,
                 exact_prompts: bool = False):
        self.source_run_id = source_run_id
        self.exact_prompts = exact_prompts
        # Un-merge before indexing, not after: a chunked agent's row must
        # become N independently-matchable, independently-consumable
        # RecordedCalls (see `_unmerge_chunked_call`) for `.match()` to ever
        # see more than one candidate for it.
        expanded, self.findings = expand_and_audit(calls)
        self.matches: list[dict] = []
        self._by_agent: dict[str, list[RecordedCall]] = {}
        for call in sorted(expanded, key=lambda c: c.row_id):
            self._by_agent.setdefault(normalise(call.agent_name), []).append(call)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- loading

    @classmethod
    def from_database(
        cls, db_path: str, *, run_id: str | None = None, agent_names: list[str] | None = None,
        exact_prompts: bool = False,
    ) -> "ResponseLibrary":
        """Load every replayable call for `run_id` (or all runs when None).

        Rows without an `input_message` predate input capture and cannot be
        matched, so they are excluded rather than matched on an empty prompt.
        """
        query = (
            "SELECT id, agent_name, run_id, timestamp, model, input_message, "
            "full_response, input_tokens, output_tokens, cost_usd, "
            "finish_reason, actual_provider, provider_requests FROM agent_logs "
            "WHERE input_message IS NOT NULL AND input_message != '' "
            "AND full_response IS NOT NULL"
        )
        params: list = []
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if agent_names:
            placeholders = ",".join("?" for _ in agent_names)
            query += f" AND agent_name IN ({placeholders})"
            params.extend(agent_names)
        query += " ORDER BY id"

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        calls = [
            RecordedCall(
                row_id=int(row["id"]),
                agent_name=str(row["agent_name"]),
                run_id=str(row["run_id"]),
                timestamp=str(row["timestamp"]),
                model=str(row["model"] or ""),
                input_message=str(row["input_message"]),
                full_response=str(row["full_response"]),
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cost_usd=(None if row["cost_usd"] is None else float(row["cost_usd"])),
                finish_reason=(row["finish_reason"] or None),
                actual_provider=(row["actual_provider"] or None),
                provider_requests=int(row["provider_requests"] or 1),
            )
            for row in rows
        ]
        return cls(calls, source_run_id=run_id, exact_prompts=exact_prompts)

    # ------------------------------------------------------------ matching

    def available(self) -> dict[str, int]:
        return {agent: len(calls) for agent, calls in self._by_agent.items()}

    def unused(self) -> list[RecordedCall]:
        return [c for calls in self._by_agent.values() for c in calls if not c.consumed]

    def match(self, agent_name: str, user_message: str) -> RecordedCall:
        """Pick and consume the recording that best fits this live prompt."""
        return match_recorded_call(self, agent_name, user_message)

    def _record_finding(self, *, kind: str, agent: str, detail: str, **extra) -> None:
        self.findings.append({"kind": kind, "agent": agent, "detail": detail, **extra})


# --------------------------------------------------------------- the patch


@contextmanager
def replay_provider_calls(library: ResponseLibrary, faults=None):
    """Replace the three provider transports with recorded-response replay.

    Everything above the transport — reservation, authorization, retry loop,
    failover, truncation detection, cost accounting, agent_logs write — is the
    real code, untouched.

    `faults` is an optional `ops.rehearsal.faults.ProviderFaultInjector`. Every
    recorded response is by definition a response that succeeded, so without
    it the retry and failover branches are unreachable offline — see that
    module for what that blind spot cost on 2026-08-31. Injected failures are
    reported as findings so a fault-injected run can never be mistaken for a
    clean one.
    """
    from src.agents.base import BaseAgent

    original = {
        "_anthropic_call": BaseAgent._anthropic_call,
        "_call_openai": BaseAgent._call_openai,
        "_call_deepseek": BaseAgent._call_deepseek,
        # The FOURTH transport, and the one that was reaching the live
        # network from inside a "replayed" session until 2026-10-01. Patching
        # only the three primary-path entry points left the two FAILOVER
        # routes live: `_try_failover` and `_try_tertiary` both call
        # `_openai_wire_call` DIRECTLY with a client they build themselves,
        # so the moment a replayed primary raised, the Portfolio Manager went
        # out to a real provider and the run ended on
        # `openai.APIConnectionError: Connection error.` [measured
        # 2026-10-01]. Replaying at the shared wire level closes both routes
        # at once and keeps the retry/failover logic above it real.
        "_openai_wire_call": BaseAgent._openai_wire_call,
    }

    def _replay(agent, model: str, user_message: str, authorize):
        # Same position as the real transports: authorization happens before
        # the "request", so a mid-session circuit trip fires here exactly as
        # it would against a live provider.
        if authorize is not None:
            authorize(model)
        # After authorization, before any "response": exactly where a real
        # transport failure surfaces. The attempt has already been counted and
        # priced by the circuit, which is the whole point — a fault that
        # skipped that would not be testing the thing that broke.
        if faults is not None:
            try:
                faults.check(agent.name, model)
            except Exception as fault:
                library.findings.append(fault.record)
                raise
        call = library.match(agent.name, user_message)
        logger.info(
            "Rehearsal: replaying %s from agent_logs row %d (recorded %s)",
            agent.name, call.row_id, call.timestamp,
        )
        return (
            call.full_response,
            call.input_tokens,
            call.output_tokens,
            call.finish_reason,
            call.reported_cost(),
        )

    def anthropic_call(self, client, model, user_message, *, authorize=None):
        return _replay(self, model, user_message, authorize)

    def openai_call(self, user_message, *, authorize=None):
        return _replay(self, self.model, user_message, authorize)

    def deepseek_call(self, user_message, *, authorize=None):
        return _replay(self, self.model, user_message, authorize)

    if faults is not None and faults.active:
        for line in faults.summary():
            logger.warning("Rehearsal FAULT INJECTION active — %s", line)

    def openai_wire_call(
        self, client, model, provider, user_message, *,
        provider_order=None, authorize=None,
    ):
        return _replay(self, model, user_message, authorize)

    BaseAgent._anthropic_call = anthropic_call
    BaseAgent._call_openai = openai_call
    BaseAgent._call_deepseek = deepseek_call
    BaseAgent._openai_wire_call = openai_wire_call
    try:
        yield library
    finally:
        for name, func in original.items():
            setattr(BaseAgent, name, func)
