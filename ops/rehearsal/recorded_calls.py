"""Recover provider calls and audit whether their history is complete.

Chunked agents store several provider transports in one logical
``agent_logs`` row. Labelled prompt/response sections let replay recover those
calls. ``provider_requests`` is a second witness: when it exceeds the
recoverable sections, attempts were collapsed out and the recording cannot
support a session verdict.

This module uses :func:`dataclasses.replace` rather than importing
``RecordedCall`` from ``replay``, avoiding an import cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

logger = logging.getLogger(__name__)

INCOMPLETE_ATTEMPTS = "incomplete_provider_attempt_recording"
CANNOT_JUDGE_FINDINGS = frozenset({
    "replay_session_mismatch",
    "missing_recorded_response",
    "low_confidence_match",
    INCOMPLETE_ATTEMPTS,
})
INCONCLUSIVE_FINDINGS = frozenset({
    "replay_session_mismatch",
    INCOMPLETE_ATTEMPTS,
})

_CHUNK_LABEL_RE = re.compile(
    r"^--- (chunk \d+/\d+|missing-symbol recovery|retry) ---$", re.MULTILINE,
)


def _split_labelled_sections(text: str) -> list[tuple[str, str]] | None:
    """Split a merged input or response into labelled real-call sections."""
    matches = list(_CHUNK_LABEL_RE.finditer(text))
    if not matches:
        return None
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end() + 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end]
        if content.endswith("\n\n"):
            content = content[:-2]
        sections.append((match.group(1), content))
    return sections


def _unmerge_chunked_call(call):
    """Recover the per-transport calls represented by one logical row."""
    messages = _split_labelled_sections(call.input_message)
    responses = _split_labelled_sections(call.full_response)
    if messages is None or responses is None:
        return [call]
    if [label for label, _ in messages] != [label for label, _ in responses]:
        logger.warning(
            "Rehearsal: agent_logs row %d (%s) has mismatched chunk markers "
            "between input_message and full_response (%s vs %s) — replaying "
            "it as one merged call rather than guessing how to un-merge it",
            call.row_id, call.agent_name,
            [label for label, _ in messages],
            [label for label, _ in responses],
        )
        return [call]

    count = len(messages)
    input_bytes = sum(len(text.encode("utf-8")) for _, text in messages) or 1
    output_bytes = sum(len(text.encode("utf-8")) for _, text in responses) or 1
    total_tokens = max(call.input_tokens + call.output_tokens, 1)
    input_left = call.input_tokens
    output_left = call.output_tokens
    cost_left = call.cost_usd
    parts = []
    for index, ((label, message), (_, response)) in enumerate(zip(messages, responses)):
        if index == count - 1:
            input_tokens, output_tokens, cost = input_left, output_left, cost_left
        else:
            input_tokens = round(call.input_tokens * len(message.encode("utf-8")) / input_bytes)
            output_tokens = round(call.output_tokens * len(response.encode("utf-8")) / output_bytes)
            input_left -= input_tokens
            output_left -= output_tokens
            cost = None if cost_left is None else round(
                call.cost_usd * (input_tokens + output_tokens) / total_tokens, 8,
            )
            if cost_left is not None:
                cost_left = round(cost_left - cost, 8)
        parts.append(replace(
            call,
            input_message=message,
            full_response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            finish_reason=call.finish_reason if index == count - 1 else "stop",
            provider_requests=1,
            part_label=f"{label} ({index + 1}/{count})",
        ))
    return parts


def expand_and_audit(calls) -> tuple[list, list[dict]]:
    """Return replayable calls and findings for collapsed provider attempts."""
    expanded = []
    findings = []
    for call in calls:
        parts = _unmerge_chunked_call(call)
        represented = len(parts)
        attempted = max(int(call.provider_requests or 0), represented)
        if attempted > represented:
            findings.append({
                "kind": INCOMPLETE_ATTEMPTS,
                "agent": call.agent_name,
                "row_id": call.row_id,
                "attempted": attempted,
                "represented": represented,
                "detail": (
                    f"agent_logs row {call.row_id} says {attempted} provider "
                    f"attempts occurred, but its retained input/response can "
                    f"reconstruct only {represented}. The missing failed or "
                    "retried transport attempts can change routing, cost and "
                    "circuit state, so this recording cannot support a "
                    "faithful session verdict"
                ),
            })
        expanded.extend(parts)
    return expanded, findings
