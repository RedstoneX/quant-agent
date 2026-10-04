"""Item 219: an old record with no examined count must not read as an empty book."""
from src.rotation import pruning_pass_lines
from src.rotation_unrecorded import examined_count_of


def test_absent_count_is_not_recorded_not_empty():
    old = {"outcome": "ok", "stage": "rotation"}
    assert examined_count_of(old) is None
    text = " ".join(pruning_pass_lines(old))
    assert "not recorded" in text
    assert "book is empty" not in text.replace("does not say the book was empty", "")


def test_written_zero_still_means_empty_book():
    rec = {"outcome": "ok", "held_examined_count": 0}
    assert examined_count_of(rec) == 0
    assert "book is empty" in " ".join(pruning_pass_lines(rec))


def test_real_count_unchanged():
    rec = {"outcome": "ok", "held_examined_count": 1, "held_examined": "AAA"}
    assert examined_count_of(rec) == 1
    assert "examined all 1 holding" in " ".join(pruning_pass_lines(rec))
