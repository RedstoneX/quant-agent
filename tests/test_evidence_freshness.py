"""The freshness reader, constructed on its own.

THE POINT OF THIS FILE: nothing here imports `src.evidence_gate`. The
reader is built from plain dictionaries of status words, and every
classification it makes is checked against the tables it was handed. If
this file ever needs the gate module back, the boundary was not real.
"""
import pytest

from src.evidence_freshness import (
    READ_ABSENT,
    READ_CARRIED,
    READ_REFRESHED,
    EvidenceFreshness,
    FreshnessReader,
    build_freshness_reader,
)

#: A made-up vocabulary on purpose: these words appear in no desk table, so
#: a reader that quietly reached for the gate's own mapping would fail here.
TABLE = {"looked": "F", "remembered": "C", "lost": "A", "stale": "C"}


def _reader(**kw):
    kw.setdefault("status_freshness", TABLE)
    kw.setdefault("expired_statuses", {"stale"})
    kw.setdefault("fresh_label", "F")
    kw.setdefault("carried_label", "C")
    kw.setdefault("absent_label", "A")
    return build_freshness_reader(**kw)


def test_the_reader_is_built_from_plain_values_and_keeps_each_one():
    built = _reader()
    assert isinstance(built, FreshnessReader)
    assert built._status_freshness == TABLE
    assert built._expired_statuses == frozenset({"stale"})
    assert (built._fresh_label, built._carried_label, built._absent_label) == (
        "F", "C", "A",
    )


def test_it_classifies_only_by_the_table_it_was_handed():
    record = _reader().read(
        {"a": "looked", "b": "remembered", "c": "lost", "d": "stale"}
    )
    assert record.fresh == ["a"]
    assert record.carried == ["b", "d"]
    assert record.absent == ["c"]
    assert record.known_out_of_date == ["d"]
    assert record.unknown == []


def test_a_word_outside_the_table_is_never_reported_as_read():
    record = _reader().read({"a": "ok"})
    assert record.unknown == ["a"] and record.fresh == []


def test_it_never_raises_on_junk():
    for junk in (None, "not a dict", 7, []):
        assert _reader().read(junk) == EvidenceFreshness()


def test_every_table_must_be_handed_over_explicitly():
    with pytest.raises(TypeError):
        build_freshness_reader()


def test_the_read_state_vocabulary_travels_with_the_record():
    record = _reader().read({"a": "looked", "b": "remembered", "c": "lost"})
    stamps = record.stamped(run_id="r1", mode="morning").seat_stamps()
    assert stamps["a"]["state"] == READ_REFRESHED
    assert stamps["b"]["state"] == READ_CARRIED
    assert stamps["c"]["state"] == READ_ABSENT
