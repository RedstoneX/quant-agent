"""A wrapped trailer is a typographic accident, not a failed declaration.

Tests for `scripts.definition_of_done.unwrap_trailers`, lifted verbatim from
`tests/test_definition_of_done.py` so that file stops growing.
"""

from __future__ import annotations

import re

from scripts import definition_of_done as dod


def test_a_wrapped_trailer_is_joined_before_any_check_reads_it():
    """Four pull requests were re-cut on 2026-09-26 for this and nothing else.

    Every check captures a trailer's value to end-of-line, so a trailer written
    across several physical lines arrived truncated to its first line — an
    `Acceptance-observable:` wrapped at column 72 read as eight words with no
    path. Because the gate reads EVERY occurrence in `git log base..HEAD`, a
    later commit could not correct it, and with force-push blocked the only
    remedy was re-cutting the branch. The work was never the problem.
    """
    wrapped = (
        "Some subject line\n"
        "\n"
        "Acceptance-observable: tests/test_definition_of_done.py fails when a\n"
        "    trailer is wrapped across several physical lines instead of one\n"
        "Objection-1: joining continuation lines could swallow the next\n"
        "    trailer and hide a missing declaration\n"
        "Response-1: REJECTED a continuation must be indented and must not itself "
        "look like a key, so the next trailer always ends the one above it\n"
    )
    joined = dod.unwrap_trailers(wrapped)
    observable = dod.trailer(joined, "Acceptance-observable")
    assert len(observable) == 1
    assert "physical lines instead of one" in observable[0]
    assert len(observable[0].split()) >= 10
    objections = dod.OBJECTION.findall(joined)
    assert len(objections) == 1
    assert "hide a missing declaration" in objections[0][1]
    assert len(dod.RESPONSE.findall(joined)) == 1


def test_unwrapping_leaves_an_unwrapped_message_byte_identical():
    """The normaliser must be invisible to every message that did not need it."""
    plain = (
        "Subject\n"
        "\n"
        "A paragraph of ordinary prose that happens to mention a ratio of 2:1\n"
        "and continues on the next line without being a trailer at all.\n"
        "\n"
        "Acceptance-observable: one physical line citing scripts/definition_of_done.py "
        "and carrying well over the ten words this check requires\n"
    )
    assert dod.unwrap_trailers(plain) == plain


def test_a_continuation_never_swallows_the_next_trailer():
    """An indented line that is itself `Key:` starts a new trailer, not a tail."""
    text = (
        "Objection-1: the first argument runs to a reasonable length here\n"
        "    Objection-2: an indented second objection is still its own trailer\n"
    )
    assert len(dod.OBJECTION.findall(dod.unwrap_trailers(text))) == 2


def test_an_unindented_continuation_is_joined_too():
    """The indented-only rule rescued none of the five PRs it shipped for.

    Git's trailer convention says a continuation is indented. Nobody writing
    these indents them, so the first version of `unwrap_trailers` described a
    convention this desk does not follow and left every blocked pull request
    exactly where it was. A rule nobody follows is not a rule.
    """
    wrapped = (
        "Subject\n"
        "\n"
        "Acceptance-observable: tests/test_definition_of_done.py refuses a\n"
        "declaration whose first physical line is too short to carry a claim\n"
    )
    values = dod.trailer(dod.unwrap_trailers(wrapped), "Acceptance-observable")
    assert len(values) == 1
    assert values[0].endswith("carry a claim")
    assert len(values[0].split()) >= 10


def test_prose_after_a_non_gate_colon_line_is_left_alone():
    """Joining is restricted to the keys this module reads.

    A commit body is full of colons. If any `Word:` line could absorb the
    sentence under it, the normaliser would be silently rewriting prose it has
    no business touching — so only the gate's own keys pull a continuation.
    """
    text = "Note: this paragraph explains the change\nand continues on a second line that must not be joined.\n"
    assert dod.unwrap_trailers(text) == text


def test_a_blank_line_ends_a_continuation():
    """Otherwise one trailer would swallow the whole rest of the message."""
    text = (
        "Objection-1: the first argument is long enough to clear the floor\n"
        "and wraps onto this line\n"
        "\n"
        "An unrelated paragraph that belongs to nobody.\n"
    )
    joined = dod.unwrap_trailers(text)
    objections = dod.OBJECTION.findall(joined)
    assert len(objections) == 1
    assert "wraps onto this line" in objections[0][1]
    assert "unrelated paragraph" not in objections[0][1]
