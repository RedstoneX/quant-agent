"""Board item 225: citations are resolved, not just counted."""

from pathlib import Path

from src.ledger_citations import broken_citations


def test_a_citation_whose_text_moved_or_changed_is_rejected(tmp_path: Path) -> None:
    """Item 225. The old guard passed any line that merely existed."""
    (tmp_path / "src").mkdir()
    module = tmp_path / "src" / "m.py"
    module.write_text("X = 1\n\n\nclass A:\n    def f(self):\n        return 2\n")

    def reasons(source: str) -> list[str]:
        ledger = {"s": {"status": "sourced", "source": source}}
        return [why for _, why, _ in broken_citations(ledger, tmp_path)]

    assert reasons("src/m.py::A.f and src/m.py::X") == []
    assert reasons("src/m.py@`return   2`") == []
    assert reasons("src/m.py:6")  # a bare line is unverifiable by construction
    assert reasons("src/m.py::A.g")  # symbol gone
    assert reasons("src/m.py@`return 3`")  # text changed
    # the file is reshuffled: lines move, the symbol citation still holds,
    # and a snippet citation that no longer exists is caught
    module.write_text("\n" * 40 + "class A:\n    def f(self):\n        return 9\n")
    assert reasons("src/m.py::A.f") == []
    assert reasons("src/m.py@`return 2`")
