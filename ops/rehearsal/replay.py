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
    # Set only when this call was recovered from a chunked agent's merged
    # row (see `_unmerge_chunked_call`) — names which real call this is
    # ("chunk 2/3 (2/4)") so a report or a MissingRecordedResponse can say
    # precisely which one, instead of just repeating the shared row_id.
    part_label: str | None = None
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


# The exact marker `analyze_batch` / `_merge_agent_results` join real calls
# behind (src/agents/tech_analyst.py) — see the module docstring's "CHUNKED
# AGENTS" section. Matched only at the start of a line so ordinary prompt or
# response content (OHLCV numbers, JSON) can never be mistaken for one: none
# of these three labels is a string tech_analyst's prompts or a JSON tech
# rating would ever legitimately produce on its own line.
_CHUNK_LABEL_RE = re.compile(
    r"^--- (chunk \d+/\d+|missing-symbol recovery|retry) ---$", re.MULTILINE,
)


def _split_labelled_sections(text: str) -> list[tuple[str, str]] | None:
    """Split merged text back into its `(label, content)` real-call parts.

    Returns None when `text` carries no chunk markers — the normal case for
    every non-chunked agent's row, and for a tech_analyst row small enough
    that `analyze_batch` never needed to chunk it.
    """
    matches = list(_CHUNK_LABEL_RE.finditer(text))
    if not matches:
        return None
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end() + 1  # skip the single \n ending the marker line
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end]
        if content.endswith("\n\n"):
            content = content[:-2]  # the "\n\n".join separator, not content
        sections.append((m.group(1), content))
    return sections


def _unmerge_chunked_call(call: RecordedCall) -> list[RecordedCall]:
    """Recover the real per-call recordings behind one merged agent_logs row.

    Falls back to `[call]` unchanged whenever `input_message` and
    `full_response` don't carry the identical label sequence — an admission
    that this row can't be reliably un-merged, not a guess dressed up as one.
    A row like that still replays as a single call exactly as it always has.
    """
    msg_sections = _split_labelled_sections(call.input_message)
    resp_sections = _split_labelled_sections(call.full_response)
    if msg_sections is None or resp_sections is None:
        return [call]
    if [label for label, _ in msg_sections] != [label for label, _ in resp_sections]:
        logger.warning(
            "Rehearsal: agent_logs row %d (%s) has mismatched chunk markers "
            "between input_message and full_response (%s vs %s) — replaying "
            "it as one merged call rather than guessing how to un-merge it",
            call.row_id, call.agent_name,
            [l for l, _ in msg_sections], [l for l, _ in resp_sections],
        )
        return [call]

    n = len(msg_sections)
    total_in_bytes = sum(len(msg.encode("utf-8")) for _, msg in msg_sections) or 1
    total_out_bytes = sum(len(resp.encode("utf-8")) for _, resp in resp_sections) or 1
    total_tokens = max(call.input_tokens + call.output_tokens, 1)

    parts: list[RecordedCall] = []
    input_left, output_left, cost_left = call.input_tokens, call.output_tokens, call.cost_usd
    for i, ((label, msg), (_, resp)) in enumerate(zip(msg_sections, resp_sections)):
        if i == n - 1:
            # Last part takes whatever's left, not its own prorated share —
            # guarantees the parts' tokens/cost sum to exactly the recorded
            # total no matter how rounding fell on the earlier parts.
            input_i, output_i, cost_i = input_left, output_left, cost_left
        else:
            in_share = len(msg.encode("utf-8")) / total_in_bytes
            out_share = len(resp.encode("utf-8")) / total_out_bytes
            input_i = round(call.input_tokens * in_share)
            output_i = round(call.output_tokens * out_share)
            input_left -= input_i
            output_left -= output_i
            if cost_left is None:
                cost_i = None
            else:
                combined_share = (input_i + output_i) / total_tokens
                cost_i = round(call.cost_usd * combined_share, 8)
                cost_left = round(cost_left - cost_i, 8)
        parts.append(RecordedCall(
            row_id=call.row_id,
            agent_name=call.agent_name,
            run_id=call.run_id,
            timestamp=call.timestamp,
            model=call.model,
            input_message=msg,
            full_response=resp,
            input_tokens=input_i,
            output_tokens=output_i,
            cost_usd=cost_i,
            # `finish_reason` is only known for the row's LAST real call —
            # it's the one `last_finish_reason` in analyze_batch's merge
            # kept. Earlier parts get the non-truncating default rather than
            # inventing a truncation signal that was never recorded for them.
            finish_reason=(call.finish_reason if i == n - 1 else "stop"),
            actual_provider=call.actual_provider,
            part_label=f"{label} ({i + 1}/{n})",
        ))
    return parts


class ResponseLibrary:
    """Recorded model responses for one run, matched to live calls."""

    def __init__(self, calls: list[RecordedCall], *, source_run_id: str | None = None):
        self.source_run_id = source_run_id
        # Un-merge before indexing, not after: a chunked agent's row must
        # become N independently-matchable, independently-consumable
        # RecordedCalls (see `_unmerge_chunked_call`) for `.match()` to ever
        # see more than one candidate for it.
        expanded: list[RecordedCall] = []
        for call in calls:
            expanded.extend(_unmerge_chunked_call(call))
        self._by_agent: dict[str, list[RecordedCall]] = {}
        for call in sorted(expanded, key=lambda c: c.row_id):
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
                "part_label": chosen.part_label,
                "run_id": chosen.run_id,
                "recorded_at": chosen.timestamp,
                "similarity": round(score, 4),
                "candidates": len(candidates),
            })
            if score < LOW_CONFIDENCE_MATCH:
                where = (
                    f"agent_logs row {chosen.row_id} {chosen.part_label}"
                    if chosen.part_label else f"agent_logs row {chosen.row_id}"
                )
                self._record_finding(
                    kind="low_confidence_match",
                    agent=agent_name,
                    detail=(
                        f"the prompt this rehearsal assembled overlaps only "
                        f"{score * 100:.0f}% with the prompt that produced the "
                        f"recorded answer ({where}, recorded {chosen.timestamp}). "
                        f"The replayed answer is being applied to a materially "
                        f"different question"
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
