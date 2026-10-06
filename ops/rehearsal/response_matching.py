"""Deterministic one-use matching of recorded model calls to replay prompts."""

from __future__ import annotations

import re


LOW_CONFIDENCE_MATCH = 0.55
WORD = re.compile(r"[A-Za-z0-9_.$%-]+")
SESSION_SUFFIXES = (
    "_morning", "_midday", "_close", "_evening", "_intra_check", "_preprocess",
)


class MissingRecordedResponse(RuntimeError):
    """A rehearsed call had no recorded response to replay."""

    status_code = 400  # missing evidence cannot be repaired by retrying


def normalise(agent_name: str) -> str:
    name = (agent_name or "").strip().lower()
    for suffix in SESSION_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


def match_recorded_call(library, agent_name: str, user_message: str):
    """Consume one matching call, or leave a precise missing-evidence finding."""
    key = normalise(agent_name)
    with library._lock:
        candidates = [c for c in library._by_agent.get(key, []) if not c.consumed]
        if not candidates:
            total = len(library._by_agent.get(key, []))
            detail = (
                f"all {total} recorded response(s) were already replayed"
                if total else "no recorded response exists"
            )
            library._record_finding(
                kind="missing_recorded_response", agent=agent_name,
                detail=detail,
                prompt_bytes=len((user_message or "").encode("utf-8")),
            )
            raise MissingRecordedResponse(
                f"no recorded response for agent '{agent_name}' "
                f"(run {library.source_run_id or 'any'}): {detail}"
            )
        if library.exact_prompts:
            candidates = [call for call in candidates
                          if call.input_message == user_message]
            if not candidates:
                library._record_finding(
                    kind="missing_exact_recorded_prompt", agent=agent_name,
                    detail="captured run has no byte-identical prompt for this call",
                )
                raise MissingRecordedResponse(
                    f"no exact recorded prompt for agent '{agent_name}'"
                )

        live_words = frozenset(WORD.findall(user_message or ""))
        # Compare indices, not calls: unmerged parts may share row IDs and
        # identical scores, and RecordedCall dataclasses are not orderable.
        keys = [
            (jaccard(live_words, c.words), -c.row_id, -i)
            for i, c in enumerate(candidates)
        ]
        best_index = max(range(len(candidates)), key=lambda i: keys[i])
        chosen = candidates[best_index]
        score = keys[best_index][0]
        chosen.consumed = True

        library.matches.append({
            "agent": agent_name, "recorded_as": chosen.agent_name,
            "row_id": chosen.row_id, "part_label": chosen.part_label,
            "run_id": chosen.run_id, "recorded_at": chosen.timestamp,
            "similarity": round(score, 4), "candidates": len(candidates),
        })
        if score < LOW_CONFIDENCE_MATCH:
            where = (
                f"agent_logs row {chosen.row_id} {chosen.part_label}"
                if chosen.part_label else f"agent_logs row {chosen.row_id}"
            )
            library._record_finding(
                kind="low_confidence_match", agent=agent_name,
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
