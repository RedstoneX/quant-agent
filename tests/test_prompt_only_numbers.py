"""Board item 107(b): a threshold in prompt prose needs a recorded status.

A figure that appears only in a prompt governs a model's behaviour without any
review. This test finds threshold-shaped figures in every sheet and requires
each to be covered by a row in `config/prompt_only_numbers.yaml`, and each row
to still match its sheet. It reads prose only; no model call.

LIMIT: this is a known-string guard. The shapes below do not catch a reworded
reintroduction (e.g. "allocate 20-30% more"); only the exact retired-figure pin
is a firm guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "config" / "prompts"
ROWS = yaml.safe_load((ROOT / "config" / "prompt_only_numbers.yaml").read_text())["numbers"]

# Shapes that state a cut-off or a sizing figure. Deliberately narrow: this
# catches the families item 107 found, and a new family is added HERE.
SHAPES = [
    r"\d+\+ aligned signals",
    r"age \d+(?:-\d+| ?\+) days",
    r"signal_age_days\s*[≥>]=?\s*\d+",
    r"(?:Forward PE|forward PE)\s*[<>]\s*\d+x",
    r"P/S\s*[<>]\s*\d+",
    r"(?:cut|halve|add|trim)\w*\s+(?:allocation\s+)?\d+(?:-\d+)?%",
    r"you MAY add \d+-\d+%",
    r"R/R\s*[≥>]\s*\d+(?:\.\d+)?\s*[—-]\s*asymmetric",
]


def _hits():
    for path in sorted(PROMPTS.glob("*.md")):
        text = path.read_text()
        for shape in SHAPES:
            for m in re.finditer(shape, text):
                yield path.name, m.group(0)


def test_every_threshold_in_a_sheet_has_a_recorded_status():
    uncovered = [
        f"{sheet}: {tok!r}"
        for sheet, tok in _hits()
        if not any(r["sheet"] == sheet and re.search(r["pattern"], tok) for r in ROWS)
    ]
    assert uncovered == [], (
        "prompt-only numbers with no row in config/prompt_only_numbers.yaml "
        "(source it, record it as arbitrary with its open question, or remove it):\n" + "\n".join(uncovered)
    )


def test_every_row_still_matches_its_sheet():
    dead = [r["id"] for r in ROWS if not re.search(r["pattern"], (PROMPTS / r["sheet"]).read_text())]
    assert dead == [], f"rows whose number is gone from the sheet, delete them: {dead}"


def test_rows_are_honest_and_complete():
    for r in ROWS:
        assert r["status"] in {"arbitrary", "sourced"}, r["id"]
        assert len(r["open_question"].split()) >= 8, r["id"]


def test_the_retired_pm_sizing_figures_stay_out_of_the_pm_sheet():
    text = (PROMPTS / "portfolio_manager.md").read_text()
    for gone in (
        r"you MAY add 20-30%",
        r"cut allocation 50%",
        r"ceilings at\s+3\.0%",
        r"Today's\s+schedule",
        r"stale\s*=\s*0\.5",
        r"× stale",
        r"Stale-signal halve",
    ):
        assert not re.search(gone, text), gone
