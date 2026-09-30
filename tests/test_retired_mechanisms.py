"""The build fails while prose still describes a mechanism the code deleted.

THE CASE THIS PINS. On 2026-09-14 the daily-loss breaker stopped liquidating
the book and started halting (docs/WORK.md item 32); `_midday_emergency_
liquidate` was deleted. That PR shipped a documentation pass — WORK.md,
INCIDENT_HISTORY.md, BOARD_NOTES.md — and touched no file under
`config/prompts/`. Three days later four places still described the deleted
behaviour, and two of them were text a paid model reads every session:
`src/agents/position_reviewer.py` assembles the reviewer's user message and
told the seat, whenever any safety net had fired that day, that the desk
performs an "emergency sell-all on −3% daily-loss breach".

Three tests here:

  1. THE BUILD CHECK (`test_no_prose_describes_a_retired_mechanism`). The one
     that fails CI.
  2. THE REGISTRY IS WELL FORMED — a malformed registry must raise, never
     quietly check nothing.
  3. SYNTHETIC DRIFT — a temporary tree carrying exactly the 2026-09-14
     sentence is detected, and the same tree with the sentence corrected is
     not. Without this, a check that scans nothing also passes.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.retired_mechanisms import (
    OPT_OUT_MARKER, REGISTRY_PATH, RegistryError, load_registry,
    resurrected_symbols, scan,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. The build check
# ---------------------------------------------------------------------------

def test_no_prose_describes_a_retired_mechanism():
    """No prompt, assembled prompt string, docstring or comment still
    describes a mechanism `config/retired_mechanisms.yaml` says is gone."""
    findings = scan(REPO_ROOT)
    assert not findings, (
        "Prose still describes a mechanism this desk deleted. The desk PAYS a "
        "model to read some of these lines.\n\n"
        + "\n\n".join(str(f) for f in findings)
        + "\n\nFix the sentence. If the mention is a deliberate historical "
        f"record (a tombstone at a deletion site, or a prompt telling a seat "
        f"that a trigger does NOT match), put `{OPT_OUT_MARKER}` on that "
        f"line — per line, never per file."
    )


def test_no_retired_symbol_has_come_back():
    """A retired symbol defined again would leave every description of it
    correct and the registry wrong — the one way this check can lie."""
    back = resurrected_symbols(REPO_ROOT)
    assert not back, (
        "These symbols are listed as retired but are defined again:\n  "
        + "\n  ".join(back)
        + "\nEither the definition is a mistake, or the registry entry must go."
    )


# ---------------------------------------------------------------------------
# 2. The registry is well formed
# ---------------------------------------------------------------------------

def test_the_live_registry_parses_and_is_not_empty():
    entries = load_registry(REGISTRY_PATH)
    assert entries, "an empty registry silently checks nothing"
    for entry in entries:
        assert entry.why.strip(), f"{entry.name} records no replacement truth"


def test_the_2026_09_14_liquidation_is_registered():
    """Pins the originating case. If this entry is ever removed, the defect
    that prompted the whole check becomes invisible again."""
    entries = {e.name: e for e in load_registry(REGISTRY_PATH)}
    entry = entries.get("daily-loss whole-book liquidation")
    assert entry is not None, (
        "the 2026-09-14 whole-book liquidation is no longer registered"
    )
    assert entry.retired == "2026-09-14"
    assert "_midday_emergency_liquidate" in entry.symbols
    assert "emergency sell-all" in entry.phrases


def _write_registry(tmp: Path, body: str) -> Path:
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    path = tmp / "config" / "retired_mechanisms.yaml"
    path.write_text(body)
    return path


def test_a_short_phrase_is_refused():
    """A broad phrase turns the check into noise, and noise gets it switched
    off. Refusing one is the only reason this check survives contact."""
    with pytest.raises(RegistryError, match="too\n?\\s*short"):
        load_registry(_write_registry(Path("/tmp"), yaml.safe_dump({
            "retired": [{
                "name": "x", "retired": "2026-09-14", "why": "y",
                "phrases": ["sell"], "allowed_in": [],
            }],
        })))


def test_an_entry_that_checks_nothing_is_refused():
    with pytest.raises(RegistryError, match="neither a symbol nor a phrase"):
        load_registry(_write_registry(Path("/tmp"), yaml.safe_dump({
            "retired": [{
                "name": "x", "retired": "2026-09-14", "why": "y",
                "allowed_in": [],
            }],
        })))


def test_a_date_from_impression_is_refused():
    """`retired:` comes from git. A month name or "last week" is refused
    rather than stored, per the standing rule about dates."""
    with pytest.raises(RegistryError, match="YYYY-MM-DD"):
        load_registry(_write_registry(Path("/tmp"), yaml.safe_dump({
            "retired": [{
                "name": "x", "retired": "mid-September", "why": "y",
                "phrases": ["emergency sell-all"], "allowed_in": [],
            }],
        })))


def test_a_missing_registry_raises_rather_than_passing():
    with pytest.raises(RegistryError, match="missing"):
        load_registry(Path("/tmp/definitely-not-a-registry-9f3a.yaml"))


# ---------------------------------------------------------------------------
# 3. Synthetic drift — the test that proves the check is not scanning nothing
# ---------------------------------------------------------------------------

_REGISTRY = yaml.safe_dump({
    "retired": [{
        "name": "daily-loss whole-book liquidation",
        "retired": "2026-09-14",
        "why": "Replaced by a halt that closes nothing.",
        "symbols": ["_midday_emergency_liquidate"],
        "phrases": ["emergency sell-all", "emergency-sell every position"],
        "allowed_in": [],
    }],
})


def _tree(tmp_path: Path, *, prompt: str, agent: str, pipeline: str) -> Path:
    root = tmp_path / "repo"
    (root / "config" / "prompts").mkdir(parents=True)
    (root / "src" / "agents").mkdir(parents=True)
    (root / "config" / "retired_mechanisms.yaml").write_text(_REGISTRY)
    (root / "config" / "prompts" / "position_reviewer.md").write_text(prompt)
    (root / "src" / "agents" / "position_reviewer.py").write_text(agent)
    (root / "src" / "pipeline.py").write_text(pipeline)
    return root


#: The real 2026-09-16 text, reproduced. `-3%` is deliberately NOT in the
#: registry used here, so this fixture proves the MECHANISM phrase alone is
#: what catches it — the case no number-based check could have found.
_DRIFTED_AGENT = textwrap.dedent('''
    def build_user_message():
        section = (
            "These sells were triggered by hard-rule safety nets (force "
            "de-lever when cash < 0; emergency sell-all on daily-loss "
            "breach) and bypassed LLM review.\\n"
        )
        return section
''')

_FIXED_AGENT = textwrap.dedent('''
    def build_user_message():
        section = (
            "These sells were triggered by a deterministic safety net and "
            "bypassed LLM review.\\n"
        )
        return section
''')

_DRIFTED_PIPELINE = textwrap.dedent('''
    def run_intra_check():
        """Only one rule: daily P&L vs loss limit. If breached,
        emergency-sell every position."""
        return {}
''')

_FIXED_PIPELINE = textwrap.dedent('''
    def run_intra_check():
        """Only one rule: daily P&L vs loss limit. If breached, HALT the
        desk. It closes, resizes and zeroes nothing."""
        return {}
''')


def test_synthetic_drift_in_an_assembled_prompt_string_is_caught(tmp_path):
    """The flagship shape: a deleted mechanism, described in words, inside a
    Python-assembled prompt — not in `config/prompts/*.md` at all."""
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=_DRIFTED_AGENT,
        pipeline=_FIXED_PIPELINE,
    )
    findings = scan(root)
    assert len(findings) == 1
    assert findings[0].path == "src/agents/position_reviewer.py"
    assert findings[0].matched == "emergency sell-all"


def test_synthetic_drift_in_a_docstring_is_caught(tmp_path):
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=_FIXED_AGENT,
        pipeline=_DRIFTED_PIPELINE,
    )
    findings = scan(root)
    assert len(findings) == 1
    assert findings[0].path == "src/pipeline.py"
    assert findings[0].matched == "emergency-sell every position"


def test_synthetic_drift_in_a_prompt_file_is_caught(tmp_path):
    root = _tree(
        tmp_path,
        prompt="The desk performs an emergency sell-all when the day is lost.\n",
        agent=_FIXED_AGENT, pipeline=_FIXED_PIPELINE,
    )
    findings = scan(root)
    assert len(findings) == 1
    assert findings[0].path == "config/prompts/position_reviewer.md"


def test_the_corrected_tree_passes(tmp_path):
    """The other half of the proof: the check is not simply always failing."""
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=_FIXED_AGENT,
        pipeline=_FIXED_PIPELINE,
    )
    assert scan(root) == []


def test_a_tombstone_line_is_exempt_but_only_that_line(tmp_path):
    """A deletion-site record is legitimate and opts out per LINE. The next
    line in the same file is still scanned — whole-file allowlisting is how
    the `run_intra_check` docstring would have stayed hidden."""
    pipeline = textwrap.dedent('''
        # `_midday_emergency_liquidate` was DELETED 2026-09-14 (retired-ok).
        def run_intra_check():
            """If breached, emergency-sell every position."""
            return {}
    ''')
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=_FIXED_AGENT, pipeline=pipeline,
    )
    findings = scan(root)
    assert len(findings) == 1, [str(f) for f in findings]
    assert findings[0].matched == "emergency-sell every position"


def test_an_identifier_is_not_mistaken_for_prose(tmp_path):
    """A retired phrase appearing as code — a dict key, a variable — is not a
    description of anything. Flagging it would be the noise that gets a check
    disabled, so only comments, docstrings and string literals are scanned."""
    agent = textwrap.dedent('''
        EMERGENCY_SELL_ALL_TAG = 1
        LEGACY = {"emergency sell-all": EMERGENCY_SELL_ALL_TAG}
    ''')
    # The dict KEY is a string literal and is correctly caught; the
    # identifier on the line above is not.
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=agent, pipeline=_FIXED_PIPELINE,
    )
    findings = scan(root)
    assert [f.line for f in findings] == [3]


def test_a_trailing_marker_exempts_a_string_literal_line(tmp_path):
    """The opt-out works on a STRING LITERAL, not only on a whole-line
    comment.

    Filed 2026-09-20 (retired item 32). `_prose_lines` hands `scan` the
    CONTENT of a literal, so a `# retired-ok` written as a trailing comment
    on the same source line could never reach the check — which is what
    `OPT_OUT_MARKER`'s own documentation promised it would. Five such
    markers were already sitting inert in `src/config.py`, reading to every
    later author as though they worked. A historical status label kept for
    replaying old runs is exactly the shape that needs this, and it cannot
    be written as a whole-line comment.
    """
    agent = (
        'LEGACY = {\n'
        '    "emergency sell-all": 1,  # retired-ok\n'
        '    "emergency sell-all too": 2,\n'
        '}\n'
    )
    root = _tree(
        tmp_path, prompt="# reviewer\n", agent=agent, pipeline=_FIXED_PIPELINE,
    )
    findings = scan(root)
    assert [f.line for f in findings] == [3], [str(f) for f in findings]


def test_the_unenforced_25pct_sizing_cut_is_registered():
    """Item 99(f). Two live sheets told two paid seats that a 25% base-
    allocation cut happens automatically on repeated `oversized` verdicts,
    and the Risk Manager's sheet used that to tell it to discount PM's
    caution. No code ever performed the cut. The prose is gone; this pins
    the phrases so it cannot come back unnoticed."""
    entries = {e.name: e for e in load_registry(REGISTRY_PATH)}
    entry = entries.get("the automatic 25% oversized base-allocation cut")
    assert entry is not None, (
        "the unenforced 25% oversized cut is no longer registered"
    )
    assert entry.retired == "2026-09-26"
    for phrase in (
        "cut every buy base 25%",
        "base allocations 25%",
        "pre-adjusts its sizing before you ever see the plan",
    ):
        assert phrase in entry.phrases, f"{phrase!r} no longer pinned"


def test_the_two_removed_sentences_would_be_caught_if_restored(tmp_path):
    """The pairing is only worth anything if it FAILS on the exact text that
    was removed. Rebuilds both sentences verbatim in a scanned location and
    asserts the live registry flags each one."""
    prompts = tmp_path / "config" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "portfolio_manager.md").write_text(
        "| `oversized` | Cut every BUY base 25%; name it in `sizing_logic` |\n"
    )
    (prompts / "risk_manager.md").write_text(
        "- **PM calibrates against YOU.** It reads your last 5 verdicts and "
        "their `reason_category` tags and pre-adjusts its sizing before you "
        "ever see the plan — 2+ `oversized` tags cut its base allocations "
        "25%, `rr_fail` makes it read range-setup payoffs more literally.\n"
    )
    findings = scan(tmp_path, registry_path=REGISTRY_PATH)
    hit = {f.path for f in findings}
    assert "config/prompts/portfolio_manager.md" in hit, findings
    assert "config/prompts/risk_manager.md" in hit, findings
