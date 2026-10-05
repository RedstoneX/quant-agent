"""Row-by-row salvage of a list-shaped model answer — standalone.

Separated out of ``src/agents/base.py`` (the agent-base split). The entry
point takes the answer text and parsing policy by value and returns a
``RowSalvage``; nothing here knows about ``AgentResult``, ``BaseAgent``, a
provider, a seat or a transport, and it imports nothing from the module it
came out of. Behaviour is unchanged from the ``AgentResult`` method it
replaces.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from src.agents.json_answer_parse import parse_json_text, repair_unquoted_keys


@dataclass(frozen=True)
class MalformedRow:
    """One element of a list-shaped answer that was not valid JSON.

    `key` is the row's identifying field (e.g. the symbol) read from the
    broken text when it is legible, else None. `reason` is the decoder's own
    message plus the offending line, short enough for one log line.
    """
    key: str | None
    reason: str


@dataclass
class RowSalvage:
    """Result of `parse_json_rows_text`: every well-formed row, plus
    every row that was present in the answer but malformed. A row in neither
    list was genuinely not returned by the model."""
    rows: list
    malformed: list[MalformedRow]


def _array_element_spans(text: str, open_idx: int) -> tuple[list[tuple[int, int | None]], int]:
    """Spans of the top-level `{...}` elements of the array opening at
    `open_idx`, and the index just past the array's close (or len(text)).

    String-aware bracket counting, with one recovery rule: string state is
    reset at every raw newline, because JSON forbids a literal newline inside
    a string (RFC 8259 section 7: control characters U+0000-U+001F must be
    escaped). A stray or missing quote therefore corrupts at most its own
    line, never the element boundaries of the rows after it. An element still
    open when the text ends is returned with end None (a cut-off answer).
    """
    spans: list[tuple[int, int | None]] = []
    depth = 0
    in_str = False
    escaped = False
    elem_start: int | None = None
    n = len(text)
    pos = open_idx + 1
    while pos < n:
        ch = text[pos]
        if ch == "\n":
            in_str = False
            escaped = False
        elif in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0 and ch == "{":
                elem_start = pos
            depth += 1
        elif ch in "}]":
            if depth == 0:
                if ch == "]":
                    return spans, pos + 1
                # Stray closer at array level: ignore it.
            else:
                depth -= 1
                if depth == 0 and elem_start is not None:
                    spans.append((elem_start, pos + 1))
                    elem_start = None
        pos += 1
    if elem_start is not None:
        spans.append((elem_start, None))
    return spans, n


def parse_json_rows_text(
    raw_text: str,
    key_field: str = "symbol",
    list_field: str | None = None,
    key_weights: Mapping[str, int] | None = None,
) -> RowSalvage | None:
    """Row-by-row parse for an agent whose answer is a LIST of objects.

    Opt-in; `parse_json_text` is unchanged for every other seat. The
    difference only shows when the whole answer is not valid JSON:
    `parse_json` then returns ONE winning fragment, which for a list
    answer means one garbled row throws away every well-formed row beside
    it (tech seat, 2026-09-17 14:31 `intra_check-26f52bf2`: five rows
    returned, one carried a bare `n/a`, one row survived; the retry lost
    ORCL and ETN the same way although both answers carried them
    well-formed). Here each element of the answer's array is parsed on
    its own: good rows are kept, broken ones are reported as
    `MalformedRow` with their `key_field` value when legible.

    `list_field`, when given (item 157): the answer is expected to be a
    wrapper OBJECT (e.g. `{"results": [...]}` — see `TechAnalystAnswer`
    in src/models.py) rather than a bare array, because a strict
    provider-side response schema must root at an object. When the full
    (or repaired) text parses to a dict carrying that key as a list, that
    list is what gets salvaged row-by-row. A bare list is still accepted
    as-is (a legacy stored answer, or a route that didn't honour the
    schema), and a dict without that key falls back to the pre-existing
    "treat the whole object as one row" behaviour — this method never
    gets stricter than it used to for a caller that passes no
    `list_field`.

    Returns None only when nothing in the answer parses at all.
    """
    def _rows_from(parsed):
        if (
            list_field is not None
            and isinstance(parsed, dict)
            and isinstance(parsed.get(list_field), list)
        ):
            return parsed[list_field]
        if (
            list_field is not None
            and isinstance(parsed, dict)
            and key_field not in parsed
        ):
            # Adversary review, 2026-09-23: a model can answer with a
            # valid JSON OBJECT under the WRONG key — a differently
            # named wrapper ("signals", "analysis") or a null under the
            # right key with the real array under another
            # ("results": null, "data": [...]). The prompt asking for a
            # bare array made this impossible; 243 real answer-chunks
            # replayed from a read-only production snapshot
            # (2026-08-17 to 2026-09-18) are all bare arrays, so this
            # path was never exercised before the wrapper schema
            # (see tests/test_tech_seat_production_replay.py). Prefer
            # any SINGLE list-of-dicts value found at the TOP LEVEL of
            # the object over treating the whole object as one row —
            # the old behaviour silently drops every real candidate in
            # the chunk with no per-symbol reason.
            #
            # `key_field not in parsed` is required first, and each
            # candidate's elements must themselves carry `key_field`
            # (2nd adversary pass, 2026-09-23): without both checks, a
            # perfectly normal SINGLE-ROW answer that happens to carry
            # any nested list-of-objects field of its own (a model
            # answering `{"symbol": "SPY", ..., "levels": [{"price":
            # 1}]}` — a shape this schema doesn't ask for today, but
            # nothing stops a future field from looking like it) would
            # have its real row thrown away in favour of that unrelated
            # nested list. Requiring `key_field` on both sides means
            # this only ever fires for something that actually looks
            # like a differently-keyed list OF ROWS, never for a
            # single row that happens to nest a list.
            #
            # When more than one such candidate exists there is no way
            # to tell which is the real one without guessing, so this
            # deliberately falls through to the same conservative
            # whole-object behaviour as before rather than picking one.
            list_candidates = [
                v for v in parsed.values()
                if isinstance(v, list) and v
                and all(
                    isinstance(e, dict) and key_field in e for e in v
                )
            ]
            if len(list_candidates) == 1:
                return list_candidates[0]
        return parsed if isinstance(parsed, list) else [parsed]

    text = raw_text.strip()
    for candidate in (text, repair_unquoted_keys(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return RowSalvage(rows=_rows_from(parsed), malformed=[])

    # Every array in the answer whose first element is an object. The
    # LAST one wins, matching parse_json's "latest correction" rule for a
    # draft-then-fix answer.
    #
    # When `list_field` is given, restrict the scan to arrays that are
    # actually the VALUE of that key (`"results": [...]`) when at least
    # one such labelled array is found. Item 157 adversary review
    # (2026-09-20): without this, a broken answer that happens to carry
    # any OTHER array-of-objects after `results` in the raw text — a
    # sibling key, a nested field, a stray self-correction fragment —
    # would win outright under the old "last array of objects anywhere"
    # rule, silently discarding the real rows. Falls back to the
    # unrestricted scan when no labelled array is found at all (a bare
    # legacy list answer, or a route that ignored the schema).
    label_positions: list[int] = []
    if list_field is not None:
        label_re = re.compile(
            r'"%s"\s*:\s*(?=\[)' % re.escape(list_field),
        )
        label_positions = [m.end() for m in label_re.finditer(raw_text)]

    def _scan_from(start_positions: list[int] | None) -> list[tuple[int, int | None]] | None:
        chosen: list[tuple[int, int | None]] | None = None
        search = 0
        while True:
            if start_positions is not None:
                remaining = [p for p in start_positions if p >= search]
                if not remaining:
                    break
                open_idx = min(remaining)
            else:
                open_idx = raw_text.find("[", search)
                if open_idx < 0:
                    break
            nxt = open_idx + 1
            while nxt < len(raw_text) and raw_text[nxt] in " \t\r\n":
                nxt += 1
            if nxt >= len(raw_text) or raw_text[nxt] != "{":
                search = open_idx + 1
                continue
            spans, search = _array_element_spans(raw_text, open_idx)
            if spans:
                chosen = spans
        return chosen

    chosen = _scan_from(label_positions) if label_positions else None
    if chosen is None:
        chosen = _scan_from(None)

    if chosen is None:
        # Not list-shaped (e.g. a single object in prose, or a wrapper
        # object whose `list_field` array itself contains no top-level
        # `[{` — an empty `"results": []` for instance): the shared
        # fragment parser's answer, still unwrapped via `list_field`.
        parsed = parse_json_text(raw_text, key_weights)
        if parsed is None:
            return None
        return RowSalvage(rows=_rows_from(parsed), malformed=[])

    key_re = re.compile(r'"%s"\s*:\s*"([^"\n]+)"' % re.escape(key_field))
    rows: list = []
    malformed: list[MalformedRow] = []
    for start, end in chosen:
        chunk = raw_text[start:end] if end is not None else raw_text[start:]
        key_match = key_re.search(chunk)
        key = key_match.group(1) if key_match else None
        if end is None:
            malformed.append(MalformedRow(
                key, "row cut off: the answer ended before the row closed",
            ))
            continue
        error: json.JSONDecodeError | None = None
        for candidate in (chunk, repair_unquoted_keys(chunk)):
            try:
                rows.append(json.loads(candidate))
                error = None
                break
            except json.JSONDecodeError as exc:
                error = error or exc
        if error is not None:
            lines = chunk.splitlines()
            bad_line = lines[error.lineno - 1].strip() if 0 < error.lineno <= len(lines) else ""
            malformed.append(MalformedRow(
                key, f"{error.msg} near {bad_line!r}",
            ))
    if not rows and not malformed:
        return None
    return RowSalvage(rows=rows, malformed=malformed)
