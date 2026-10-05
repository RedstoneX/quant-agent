"""Pick the JSON answer out of a model's reply text — standalone.

Separated out of ``src/agents/base.py`` (the agent-base split). Every
function takes the answer text and any agent-shape policy by value; nothing
here knows about ``AgentResult``, ``BaseAgent``, a provider, a seat or a
transport, and it imports nothing from the module it came out of. Behaviour
is unchanged from the ``AgentResult`` methods it replaces.
"""

import json
import logging
import re
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def shape_score(parsed, key_weights: Mapping[str, int]) -> int:
    """How 'agent-output shaped' a JSON candidate looks. Higher is better."""
    # A top-level LIST is a first-class agent shape: tech_analyst returns
    # an array of per-symbol analyses (tech_analyst.py: `items = parsed if
    # isinstance(parsed, list) else [parsed]`). Scoring it 0 meant that
    # whenever the model wrapped the array in ANY prose (so the clean
    # json.loads happy path missed), the candidate scan compared the array
    # (score 0) against each of its own elements (score > 0) and returned
    # the LAST ELEMENT — silently discarding every other symbol's analysis
    # in the chunk. Score the container by the SUM of its elements so it
    # strictly outranks any single element it contains (2026-07-16 audit;
    # reproduced: a 3-analysis array returned 1 dict).
    if isinstance(parsed, list):
        return sum(shape_score(item, key_weights) for item in parsed)
    if not isinstance(parsed, dict):
        return 0
    keys = set(parsed.keys())
    return sum(
        weight
        for key, weight in key_weights.items()
        if key in keys
    )


def repair_unquoted_keys(text: str) -> str:
    """Fix one specific, narrow JSON syntax defect: an object key missing
    its OPENING quote while the closing quote survives, e.g.
    `  pm_briefing": "value"` instead of `  "pm_briefing": "value"`.

    Confirmed from a real production payload (news_analyst,
    2026-09-02 19:30 ET — see docs/INCIDENT_HISTORY.md): the model
    dropped exactly one opening quote on an interior top-level key.
    Because Anthropic's pretty-printed JSON always starts a key on its
    own line, and a JSON string can never contain a literal newline, any
    line beginning with a bare identifier immediately followed by `":`
    is unambiguously a key position — never the middle of a string
    value. Deliberately does NOT attempt to repair anything else
    (trailing commas, unescaped quotes inside values, etc.) — those are
    different defects with different failure signatures we have not
    observed in production; guessing a fix for an unobserved failure
    mode is exactly what docs/WORK.md's DATA QUALITY AUDIT warned
    against.
    """
    return re.sub(
        r'(?m)^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)":',
        r'\1"\2":',
        text,
    )


def parse_json_text(
    raw_text: str,
    key_weights: Mapping[str, int] | None = None,
) -> dict | list | None:
    key_weights = key_weights or {}
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
        # Full-text parse wins outright if it's a dict/list; no candidate
        # search needed. This is the happy path — LLM returned clean JSON.
        return parsed
    except json.JSONDecodeError:
        pass

    # One narrow, evidence-backed repair attempt on the full text before
    # falling back to fragment scanning below. A repaired FULL parse
    # recovers the whole report; the fragment scanner below can only
    # ever recover the largest surviving PIECE of a broken object —
    # exactly what turned a single missing quote character into a
    # "4 required top-level fields missing" structural failure in
    # production (2026-09-02).
    try:
        parsed = json.loads(repair_unquoted_keys(text))
        return parsed
    except json.JSONDecodeError:
        pass

    # Each candidate carries its source SPAN (start, end in raw_text) so
    # nested fragments can be recognized. (score, size, idx, span, parsed)
    candidates: list[tuple[int, int, int, tuple[int, int], dict | list]] = []
    # idx preserves source order so we can break ties predictably.
    idx = 0
    # Fenced ```json blocks — highest trust.
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", raw_text, re.DOTALL):
        fenced = match.group(1).strip()
        try:
            parsed = json.loads(fenced)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(repair_unquoted_keys(fenced))
            except json.JSONDecodeError:
                continue
        candidates.append((
            shape_score(parsed, key_weights), len(json.dumps(parsed)), idx,
            match.span(1), parsed,
        ))
        idx += 1

    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw_text):
        if ch not in "{[":
            continue
        try:
            parsed, end = decoder.raw_decode(raw_text[i:])
        except json.JSONDecodeError:
            continue
        candidates.append((
            shape_score(parsed, key_weights), len(json.dumps(parsed)), idx,
            (i, i + end), parsed,
        ))
        idx += 1

    # Nested-fragment filter: a candidate STRICTLY contained inside a
    # larger candidate is part of that candidate's content, not a later
    # "correction" of it — the recency tie-break below was designed for
    # DISJOINT draft-then-fix fragments. Without this, the PM's inner
    # `targets` array (5 pts/symbol) outranked the very object that
    # contained it once the plan reached ≥8 names, and the entire
    # morning decision was destroyed by a fragment of itself
    # (2026-08-17/20 production incident; see EXPECTED_AGENT_KEY_WEIGHTS
    # note). A container that itself looks like agent output (score > 0)
    # therefore always wins over its own fragments, REGARDLESS of the
    # fragments' scores. The only nested fragment worth keeping is one
    # inside a score-0 container — the prose-wrapper case
    # (e.g. {"thinking": ..., "answer": {...}}), where the wrapper has
    # no recognizable agent shape and the payload is the real output.
    def _strictly_inside(inner: tuple[int, int], outer: tuple[int, int]) -> bool:
        return (
            outer[0] <= inner[0] and inner[1] <= outer[1]
            and (outer[0] < inner[0] or inner[1] < outer[1])
        )

    filtered = [
        c for c in candidates
        if not any(
            other is not c
            and other[0] > 0
            and _strictly_inside(c[3], other[3])
            for other in candidates
        )
    ]
    candidates = filtered or candidates

    if candidates:
        max_shape = max(item[0] for item in candidates)
        if max_shape > 0:
            # Once something looks like a real agent output, prefer the
            # latest correction over an earlier larger draft.
            shaped = [item for item in candidates if item[0] == max_shape]
            return max(shaped, key=lambda item: (item[2], item[1]))[4]

        # If nothing has recognizable agent keys, fall back to the largest
        # valid JSON fragment and use recency only as a tiebreaker.
        return max(candidates, key=lambda item: (item[1], item[2]))[4]

    logger.warning("Failed to parse agent response as JSON: %s", raw_text[:200])
    return None
