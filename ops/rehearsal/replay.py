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
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Below this Jaccard overlap the replayed answer is reported as answering a
# materially different question than the one the rehearsed pipeline asked.
LOW_CONFIDENCE_MATCH = 0.55

_WORD = re.compile(r"[A-Za-z0-9_.$%-]+")

# `agent_logs.agent_name` is written by the call site, which sometimes appends
# the session (`news_analyst_morning`, `earnings_analyst_preprocess`), while
# `BaseAgent.name` never does. Recordings are indexed under both forms.
_SESSION_SUFFIXES = (
    "_morning", "_midday", "_close", "_evening", "_intra_check", "_preprocess",
)


class MissingRecordedResponse(RuntimeError):
    """A rehearsed call had no recorded response to replay."""

    # Read by `src.agents.base._is_retryable`: any 4xx other than 429 is
    # fast-failed rather than retried.
    status_code = 400


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
    consumed: bool = False
    _words: frozenset[str] | None = field(default=None, repr=False, compare=False)

    @property
    def words(self) -> frozenset[str]:
        if self._words is None:
            self._words = frozenset(_WORD.findall(self.input_message or ""))
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


def _normalise(agent_name: str) -> str:
    name = (agent_name or "").strip().lower()
    for suffix in _SESSION_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


class ResponseLibrary:
    """Recorded model responses for one run, matched to live calls."""

    def __init__(self, calls: list[RecordedCall], *, source_run_id: str | None = None):
        self.source_run_id = source_run_id
        self._by_agent: dict[str, list[RecordedCall]] = {}
        for call in sorted(calls, key=lambda c: c.row_id):
            self._by_agent.setdefault(_normalise(call.agent_name), []).append(call)
        self.findings: list[dict] = []
        self.matches: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- loading

    @classmethod
    def from_database(
        cls, db_path: str, *, run_id: str | None = None, agent_names: list[str] | None = None,
    ) -> "ResponseLibrary":
        """Load every replayable call for `run_id` (or all runs when None).

        Rows without an `input_message` predate input capture and cannot be
        matched, so they are excluded rather than matched on an empty prompt.
        """
        query = (
            "SELECT id, agent_name, run_id, timestamp, model, input_message, "
            "full_response, input_tokens, output_tokens, cost_usd, "
            "finish_reason, actual_provider FROM agent_logs "
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
            )
            for row in rows
        ]
        return cls(calls, source_run_id=run_id)

    # ------------------------------------------------------------ matching

    def available(self) -> dict[str, int]:
        return {agent: len(calls) for agent, calls in self._by_agent.items()}

    def unused(self) -> list[RecordedCall]:
        return [c for calls in self._by_agent.values() for c in calls if not c.consumed]

    def match(self, agent_name: str, user_message: str) -> RecordedCall:
        """Pick and consume the recording that best fits this live prompt."""
        key = _normalise(agent_name)
        with self._lock:
            candidates = [c for c in self._by_agent.get(key, []) if not c.consumed]
            if not candidates:
                total = len(self._by_agent.get(key, []))
                detail = (
                    f"all {total} recorded response(s) were already replayed"
                    if total else "no recorded response exists"
                )
                self._record_finding(
                    kind="missing_recorded_response",
                    agent=agent_name,
                    detail=detail,
                    prompt_bytes=len((user_message or "").encode("utf-8")),
                )
                raise MissingRecordedResponse(
                    f"no recorded response for agent '{agent_name}' "
                    f"(run {self.source_run_id or 'any'}): {detail}"
                )

            live_words = frozenset(_WORD.findall(user_message or ""))
            scored = [(_jaccard(live_words, c.words), -c.row_id, c) for c in candidates]
            scored.sort(reverse=True)
            score, _, chosen = scored[0]
            chosen.consumed = True

            self.matches.append({
                "agent": agent_name,
                "recorded_as": chosen.agent_name,
                "row_id": chosen.row_id,
                "run_id": chosen.run_id,
                "recorded_at": chosen.timestamp,
                "similarity": round(score, 4),
                "candidates": len(candidates),
            })
            if score < LOW_CONFIDENCE_MATCH:
                self._record_finding(
                    kind="low_confidence_match",
                    agent=agent_name,
                    detail=(
                        f"the prompt this rehearsal assembled overlaps only "
                        f"{score * 100:.0f}% with the prompt that produced the "
                        f"recorded answer (agent_logs row {chosen.row_id}, "
                        f"recorded {chosen.timestamp}). The replayed answer is "
                        f"being applied to a materially different question"
                    ),
                    similarity=round(score, 4),
                )
        return chosen

    def _record_finding(self, *, kind: str, agent: str, detail: str, **extra) -> None:
        self.findings.append({"kind": kind, "agent": agent, "detail": detail, **extra})


# --------------------------------------------------------------- the patch


@contextmanager
def replay_provider_calls(library: ResponseLibrary):
    """Replace the three provider transports with recorded-response replay.

    Everything above the transport — reservation, authorization, retry loop,
    failover, truncation detection, cost accounting, agent_logs write — is the
    real code, untouched.
    """
    from src.agents.base import BaseAgent

    original = {
        "_anthropic_call": BaseAgent._anthropic_call,
        "_call_openai": BaseAgent._call_openai,
        "_call_deepseek": BaseAgent._call_deepseek,
    }

    def _replay(agent, model: str, user_message: str, authorize):
        # Same position as the real transports: authorization happens before
        # the "request", so a mid-session circuit trip fires here exactly as
        # it would against a live provider.
        if authorize is not None:
            authorize(model)
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

    BaseAgent._anthropic_call = anthropic_call
    BaseAgent._call_openai = openai_call
    BaseAgent._call_deepseek = deepseek_call
    try:
        yield library
    finally:
        for name, func in original.items():
            setattr(BaseAgent, name, func)
