"""The gate. A new trade-governing number with no source fails the build.

`pytest` is the check branch protection requires, so this file — not a
reporting script somebody runs by hand — is what makes the no-arbitrary-numbers
rule mechanical. Read `src/number_sources.py`'s docstring for the scope rule
and for the honest list of what this cannot catch.

The tests below are in two groups. The first is the gate itself, against the
live tree. The second proves each failure mode actually fires, using synthetic
fixtures — because a gate nobody has seen fail is indistinguishable from a
gate that passes everything, which is how the desk's `Adversary:` check became
theatre once already.

A standing caution, because it has already happened here: every rule in this
file tests that a justification EXISTS in an openable shape. None of them
tests that one is TRUE. The first version of this ledger's flagship entry was
false in four places and passed every test below.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.number_sources import (
    ARBITRARY_REQUIRED_FIELDS,
    MAX_ARBITRARY_ENTRIES,
    MAX_UNSCOPED_NUMERIC_SITES,
    NEUTRAL_VALUES,
    SCOPED_CONFIG_CLASSES,
    SCOPED_PATHS,
    audit,
    broken_citations,
    collect_sites,
    collect_unscoped_sites,
    deployed_values,
    load_ledger,
)

# --------------------------------------------------------------------------
# The gate, against the live tree.
# --------------------------------------------------------------------------


def test_every_trade_governing_number_is_accounted_for() -> None:
    """THE GATE. Every numeric definition site in scope has a ledger entry,
    the entry's value still matches the code AND the deployed YAML, every
    claimed source is openable, every arbitrary number carries its open
    question and its cost, and no derivation has outlived its base.

    If this fails on your branch you have added or moved a number that
    governs a trade. Add it to `config/number_ledger.yaml` with where it came
    from. If nothing backs it, say `arbitrary` — and note that raising
    `MAX_ARBITRARY_ENTRIES` to fit it is an owner decision, not a build fix.
    """
    problems = audit()
    assert not problems, "\n".join(
        ["unsourced or unaccounted trade-governing numbers:", ""]
        + [f"  {p}" for p in problems]
    )


def test_the_ratified_minimum_risk_floor_is_in_scope_at_every_site() -> None:
    """The 0.50% minimum-risk floor. Every trade plan sized under it is DENIED
    OUTRIGHT rather than shrunk, and it is owner-ratified (docs/OUTCOME.md:84,
    config/settings.yaml:751, commit 75c02335 of 2026-08-27).

    It lives at THREE definition sites, and the third is the point of this
    test: `RiskConfig.min_position_risk_pct`'s default is bound to a NAME, so
    the first version of the scanner could not see it at all. The LIVE floor
    was invisible to the gate that was written around it, and the ledger
    entry that named it said the number existed in exactly one place.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.portfolio_constructor.ConstructorConfig.min_risk_pct" in ids
    assert "src.risk.constants.STARTER_POSITION_RISK_PCT" in ids
    assert "src.config.RiskConfig.min_position_risk_pct" in ids


def test_a_default_bound_to_a_name_is_still_a_site() -> None:
    """The one-line evasion. A `*Config` default written as a NAME rather than
    a literal used to return None from `_numeric` and disappear — so pointing
    a scoped field at a constant in an unscoped module removed any number from
    the gate. Constant arithmetic (`5 * 366`) did the same.
    """
    sites = {s.site_id: s.value for s in collect_sites()}
    assert sites["src.config.RiskConfig.max_position_risk_pct"] == 5.0
    assert sites["src.config.RiskConfig.min_reward_risk_after_widening"] == 1.5
    assert sites["src.config.SmartMoneyConfig.insider_history_retention_days"] == 5 * 366


def test_the_atr_unit_is_in_scope_not_only_its_multipliers() -> None:
    """`ATR_PERIOD` was out of scope while every ATR multiple in the ledger was
    in it. Watching the multiplier and not the unit is not a boundary.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.data.technical.ATR_PERIOD" in ids
    assert "src.data.levels.PIVOT_WINDOW" in ids
    assert "src.pipeline_stages.MAX_ENTRY_SLIPPAGE_BPS" in ids
    assert "src.verdicts.CONVICTION_SCORE['medium']" in ids


def test_one_atr_is_not_treated_as_an_identity() -> None:
    """`1.0` was blanket-excluded as "the identity, not a setting", with the
    claim that every audited number fell outside the excluded set. Measured
    false: the hard floor under every stop this desk sets is exactly 1.0 ATR.
    """
    assert 1.0 not in NEUTRAL_VALUES
    assert -1.0 not in NEUTRAL_VALUES
    ids = {site.site_id for site in collect_sites()}
    assert "src.config.RiskConfig.absolute_min_stop_atr_multiple" in ids
    assert "src.portfolio_constructor.ConstructorConfig.absolute_min_stop_atr_multiple" in ids
    assert "src.config.CashSweepConfig.reserve_pct" in ids
    assert "src.risk.exit_guard.NOISE_BAND_ATR_MULTIPLE" in ids


def test_stop_width_scalers_inside_a_tuple_are_sites() -> None:
    """The stop-width scalers live as numbers inside a tuple of pairs, not as
    bare field defaults, and they multiply into every stop distance. A scanner
    that only reads top-level defaults would not see them.
    """
    ids = {site.site_id for site in collect_sites()}
    assert "src.portfolio_constructor.ConstructorConfig.stop_atr_setup_scale[1][1]" in ids
    assert "src.portfolio_constructor.ConstructorConfig.stop_atr_regime_scale[0][1]" in ids


def test_result_dataclasses_are_not_sites() -> None:
    """The `*Config` rule is what keeps the signal alive. Result and DTO
    dataclasses in the same scoped files carry numeric defaults too, and
    flagging them would bury the numbers that matter — which is precisely the
    failure that made the earlier whole-codebase idea unworkable.
    """
    ids = {site.site_id for site in collect_sites()}
    assert not [i for i in ids if ".GrossCeilingOutcome." in i]
    assert not [i for i in ids if ".PortfolioVolEstimate." in i]


def test_an_in_file_alias_is_not_a_second_site() -> None:
    """One number with two names in the same file is one number. Ledgering it
    twice is exactly how the flagship entry came to assert that a number
    existed in one place while this file listed it in two.
    """
    ids = {site.site_id for site in collect_sites()}
    assert not [i for i in ids if "_EARNINGS_XBRL_COMPARABLE_FIELDS" in i]


def test_the_arbitrary_count_is_an_equality_not_a_ceiling() -> None:
    """Rule 5. As a CEILING the ratchet rewarded deletion: move a trade
    constant into an unscoped file, delete its ledger row, and the build went
    green while the headline arbitrary count FELL — the metric improving while
    the number became less visible than before the gate existed.

    As an equality, a row cannot leave this ledger without the count being
    edited in the same commit. LOWER it when a number is genuinely sourced.
    Raising it records an owner decision.
    """
    ledger = load_ledger()
    arbitrary = [e for e in ledger.values() if e.get("status") == "arbitrary"]
    assert len(arbitrary) == MAX_ARBITRARY_ENTRIES
    assert MAX_ARBITRARY_ENTRIES == 143, (
        "the ratchet moved; if a number was sourced, lower it and say which. "
        "86 -> 87 on 2026-09-18: `max_filings_per_refresh` was recorded as "
        "not-trade-governing, and that day the cap binding is what refused a "
        "trading decision -- a misclassification corrected, not a number added. "
        "87 -> 88 the same day: `refresh_deadline_s` carried the identical "
        "falsified sentence and was what the intraday freshness check ran out "
        "of while deciding whether the tick could decide. "
        "88 -> 106 on 2026-09-19, board item 130: scoping "
        "src/execution/broker.py, src/coverage_watchdog.py, src/pipeline.py "
        "and src/agents admitted 47 new sites, 18 of them arbitrary -- see "
        "src/number_sources.py's MAX_ARBITRARY_ENTRIES comment for the count "
        "by source. "
        "106 -> 146 on 2026-09-19: the scanner learned parameter defaults, "
        "attributes on any class and near-one inline multipliers; 98 live "
        "sites became visible, 40 of them arbitrary. No number was added. "
        "146 -> 147 on 2026-09-20, board item 124 adversary review: "
        "`src.data.smart_money_cluster.MIN_PURCHASE_CLUSTER_INSIDERS` was "
        "`sourced` against an SSRN URL that returns HTTP 403 to everyone; "
        "relabelled `arbitrary` because a citation nobody can open is not a "
        "source under this ledger's own rule. No value changed. "
        "147 -> 148 the same day, cross-symbol crowd-out fix: a genuinely new "
        "number, `src.data.smart_money_cluster.MAX_CLUSTER_RESERVED_SLOTS` "
        "(the reserved-slot bound for board item 124's fix), recorded "
        "honestly as `arbitrary` with its open question stated rather than "
        "presented as measured. "
        "148 -> 142 on 2026-09-20, retired board item 32: six rows left with "
        "the account-level loss alarms the owner removed. "
        "142 -> 143 on 2026-09-23, board item 180: "
        "`src.data.technical.LONGEST_INDICATOR_WINDOW` was `sourced` on the "
        "200-day moving average being a standard published trend reference, "
        "which sources the MA WINDOW and not the second use of the same "
        "constant -- the constructor's outright refusal of any listing under "
        "200 bars, for which no citation exists. One status per site, so the "
        "row takes the weaker use's status and the split is written into its "
        "note. No value changed."
    )


def test_the_arbitrary_count_counts_numbers_not_rows() -> None:
    """A mirrored constant is ONE number with two definition sites. Recording
    both as `arbitrary` would inflate the count and let the headline metric be
    improved by consolidating files rather than by sourcing anything, so a
    mirror is recorded as `derived` and its base-drift is checked.
    """
    ledger = load_ledger()
    mirrors = {
        "src.portfolio_constructor.ConstructorConfig.min_stop_atr_multiple":
            "src.config.RiskConfig.min_stop_atr_multiple",
        "src.pipeline_stages.MAX_ENTRY_SLIPPAGE_BPS":
            "src.config.ExecutionConfig.max_entry_slippage_bps",
        "src.data.levels.MAX_REACH_ATR_MULTIPLE":
            "src.config.RiskConfig.max_target_reach_atr_multiple",
    }
    for site_id, base in mirrors.items():
        assert ledger[site_id]["status"] == "derived", site_id
        assert ledger[site_id]["derived_from"] == base, site_id

    values = [e["value"] for e in ledger.values() if e.get("status") == "arbitrary"]
    assert len(values) == MAX_ARBITRARY_ENTRIES


def test_every_arbitrary_entry_carries_its_debt() -> None:
    """Rule 3 for `arbitrary`. It used to require nothing at all — no note, no
    owner, no date — which made the honest-but-unsourced status the cheapest
    one in the file to write. That is backwards. `docs/OUTCOME.md`'s outcome-3
    clause already demands the question and the cost; the schema now does too.
    """
    ledger = load_ledger()
    for site_id, entry in ledger.items():
        if entry.get("status") != "arbitrary":
            continue
        for field in ARBITRARY_REQUIRED_FIELDS:
            assert str(entry.get(field) or "").strip(), f"{site_id} has no {field}"


def test_every_source_is_openable_by_a_non_author() -> None:
    """Rule 3 for `sourced`/`instrument`. Prose is not falsifiable. All four
    false claims in this ledger's first flagship entry were prose, and all
    four were one grep from being disproved.
    """
    import re

    ledger = load_ledger()
    for site_id, entry in ledger.items():
        if entry.get("status") not in ("sourced", "instrument"):
            continue
        source = str(entry.get("source") or "")
        openable = re.search(r"https?://\S+", source) or re.search(
            r"\b[\w./-]+\.(?:py|yaml|yml|md|json|toml):\d+", source
        )
        assert openable, f"{site_id} cites prose with nothing to open"


def test_the_deployed_value_is_checked_and_not_only_the_code_default() -> None:
    """Rule 2b, and the largest blind spot the first version had. This ledger
    records the CODE DEFAULT; for a `src.config.*Config` field the number the
    desk trades comes from `config/settings.yaml`. `risk.max_position_risk_pct`
    could be edited from 5 to 10 with the gate entirely silent.
    """
    deployed = deployed_values()
    assert deployed["src.config.RiskConfig.max_position_risk_pct"] == 5.0
    ledger = load_ledger()
    routed = [k for k in deployed if k in ledger]
    assert len(routed) >= 50, f"only {len(routed)} sites routed; the mapping broke"
    for site_id in routed:
        assert abs(deployed[site_id] - float(ledger[site_id]["value"])) < 1e-12, site_id


def test_every_repo_citation_in_the_ledger_resolves() -> None:
    """Rule 7, and the cheapest possible defence against the failure that made
    this gate's own rework necessary. It cannot check that a citation SAYS what
    an entry claims — nothing can — but a path that does not exist, or a line
    past the end of a file, is a citation nobody opened.

    It earned its place immediately: it caught an invented
    `docs/FRACTIONAL_TRADING.md` in a `source` written during the rework that
    added this rule.
    """
    broken = broken_citations(load_ledger(), Path(__file__).resolve().parent.parent)
    assert not broken, "\n".join(f"  {s}: cites {c} - {w}" for s, w, c in broken)


def test_scope_has_not_silently_narrowed() -> None:
    """A scoped path or config class that stopped existing would make the gate
    pass by covering less. `collect_sites` raises on a missing path; this
    pins the scoped config classes for the same reason.
    """
    import src.config as config_module

    for name in SCOPED_CONFIG_CLASSES:
        assert hasattr(config_module, name), f"{name} left src/config.py"
    assert "src/risk" in SCOPED_PATHS
    assert "src/portfolio_constructor.py" in SCOPED_PATHS
    assert "src/data/technical.py" in SCOPED_PATHS
    assert "src/data/levels.py" in SCOPED_PATHS
    # Board item 130: broker.py IS the broker order.
    assert "src/execution/broker.py" in SCOPED_PATHS
    assert "src/execution/stop_repair.py" in SCOPED_PATHS
    assert "src/coverage_watchdog.py" in SCOPED_PATHS
    assert "src/pipeline.py" in SCOPED_PATHS
    assert "src/agents" in SCOPED_PATHS


def test_a_new_constant_outside_scope_cannot_arrive_silently() -> None:
    """The other half of scope, and the half that was missing. A test can pin
    that a hand-kept list does not SHRINK; nothing pinned that it was
    COMPLETE. Counting the constants outside it turns "somebody should widen
    scope" into a build failure the day a new one appears — and closes the
    move where an in-scope number is parked in an unscoped file.
    """
    unscoped = collect_unscoped_sites()
    assert len(unscoped) <= MAX_UNSCOPED_NUMERIC_SITES, (
        f"{len(unscoped)} unscoped module-level constants, ceiling is "
        f"{MAX_UNSCOPED_NUMERIC_SITES}. If the new one governs a trade, scope "
        f"its module and ledger it. If not, raise the ceiling and say which."
    )
    assert MAX_UNSCOPED_NUMERIC_SITES == 152, (
        "151 -> 152 on 2026-09-24: +1 for "
        "src.margin_interest.MAX_LOOKBACK_MONTHS (6), the owner's own ask "
        "for how many months back the cumulative margin-interest view "
        "(this week/current month/up to 6 months/all-time) looks. It bounds "
        "a presentation window, not any trade decision. "
        "150 -> 151 on 2026-09-23: +1 for "
        "src.margin_interest.MAX_CALENDAR_LOOKAHEAD_DAYS (7), the safety "
        "bound on the forward calendar walk behind the owner-facing "
        "margin-interest ESTIMATE (how many calendar days a Friday debit is "
        "carried). It bounds a Telegram/dashboard estimate and degrades to 1 "
        "when exhausted; it never decides, sizes, prices or exits a trade. "
        "149 -> 150 on 2026-09-23: +1 for "
        "src.data.event_calendar.RELEASE_SCHEDULE_LOOKAHEAD_DAYS, the width "
        "of the one FRED release-dates request per release. It is the fetch "
        "window, not the event horizon -- get_upcoming_events still filters "
        "to horizon_days -- so it governs what the desk can SEE, not what it "
        "trades. "

        "147 -> 149 on 2026-09-23, the three-route failover ladder: +2 for "
        "`src.llm_route_journal._DEFAULT_DB_RELATIVE`'s companions in that "
        "new module (the journal's SQLite timeout and its read_events page "
        "size). Both are plumbing on a durable log of which LLM road "
        "answered; neither decides, sizes, prices or exits a trade. The four "
        "numbers the same change added to src/agents/base.py are NOT here -- "
        "that module is in SCOPED_PATHS and they carry ledger entries. "
        "145 -> 147 on 2026-09-19: +2 for src/number_sources.py's own "
        "FACTOR_BAND, the scanner's classifier band, not a trade number. "

        "192 -> 145 on 2026-09-19, board item 130: src/execution/broker.py, "
        "src/coverage_watchdog.py, src/pipeline.py and src/agents moved into "
        "SCOPED_PATHS and their 47 sites now carry ledger entries instead of "
        "sitting in this count."
    )


# --------------------------------------------------------------------------
# Each failure mode, proven to fire.
# --------------------------------------------------------------------------


def _fixture(tmp_path: Path, module_src: str, ledger_src: str) -> tuple[Path, Path]:
    """A miniature repo: one scoped module plus a ledger, so each rule can be
    tripped in isolation without touching the real tree.

    Rules 5 (the arbitrary ratchet) and 6 (the unscoped sentinel) are
    properties of the real ledger and the real tree, so `audit` skips them for
    a fixture ledger. They are pinned against the live tree above instead.
    """
    (tmp_path / "src" / "risk").mkdir(parents=True)
    (tmp_path / "src" / "risk" / "rules.py").write_text(textwrap.dedent(module_src))
    (tmp_path / "src" / "config.py").write_text("class RiskConfig:\n    pass\n")
    for entry in SCOPED_PATHS:
        target = tmp_path / entry
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix == ".py":
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger.yaml"
    ledger.write_text(textwrap.dedent(ledger_src))
    return tmp_path, ledger


def _kinds(root: Path, ledger: Path) -> set[str]:
    return {p.kind for p in audit(repo_root=root, ledger_path=ledger)}


#: A complete `arbitrary` entry, for fixtures that are testing some other rule.
_DEBT = """
            note: nothing behind it
            open_question: what heat does this account actually survive?
            cost_while_unanswered: the ceiling binds on a round number
"""


def test_a_new_number_with_no_entry_fails() -> None:
    """Rule 1, COVERAGE — the whole point of the gate."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        "numbers: []\n",
    )
    assert "unsourced" in _kinds(root, ledger)


def test_changing_a_number_without_touching_its_entry_fails() -> None:
    """Rule 2, VALUE — a number may not outlive its recorded justification."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 3.0
            status: arbitrary
        """
        + _DEBT,
    )
    assert "value-drift" in _kinds(root, ledger)


def test_claiming_a_source_without_writing_one_fails() -> None:
    """Rule 3, GROUNDS — `sourced` is not a word you may just type."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: "   "
        """,
    )
    assert "no-source" in _kinds(root, ledger)


def test_a_source_a_reader_cannot_open_fails() -> None:
    """Rule 3 again, and the rule that answers the worst finding against the
    first version of this gate. A confident paragraph passed; a paragraph is
    what was false in four places. A `source` must be a URL or a `file:line`.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: >-
              Read off the instrument, as is well established in the
              literature, and confirmed by the desk's own measurement.
        """,
    )
    assert "unfalsifiable-source" in _kinds(root, ledger)


def test_a_source_with_a_file_and_line_passes() -> None:
    """The other half: a citation a non-author can actually open is enough.
    A rule that refused everything would be routed around inside a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: sourced
            source: the derivation at src/risk/rules.py:12
        """,
    )
    assert not _kinds(root, ledger)


def test_an_arbitrary_number_with_no_open_question_fails() -> None:
    """`arbitrary` used to be the cheapest field in the ledger: no note, no
    owner, no date, no question. It must be the most expensive, because it is
    a debt the desk is carrying.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
            note: nothing behind it
        """,
    )
    assert "incomplete-debt" in _kinds(root, ledger)


def test_an_arbitrary_number_with_no_stated_cost_fails() -> None:
    """The second half of the debt: what the desk pays while the question is
    open. Without it an arbitrary entry cannot be prioritised against any
    other, which is how twenty of them sat live for a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "MAX_HEAT_PCT = 4.2\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
            note: nothing behind it
            open_question: what heat does this account actually survive?
        """,
    )
    assert "incomplete-debt" in _kinds(root, ledger)


def test_a_derivation_whose_base_moved_fails() -> None:
    """Rule 4, BASE DRIFT — the class the brief asked about by name.

    A number is derived from a base and both are recorded. The base then
    changes, as `min_stop_atr_multiple` did on 2026-09-10 (commit 0088328c,
    3.0 -> 2.5). The derivation no longer describes the live geometry, so the
    derived number is arbitrary again, and the build says so instead of
    letting a stale justification sit there looking respectable.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
            open_question: what stop width does this desk's own MAE support?
            cost_while_unanswered: every unbacked stop scales off it
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 1.5
        """,
    )
    assert "base-drift" in _kinds(root, ledger)


def test_a_derivation_whose_base_still_holds_passes() -> None:
    """The other half of rule 4: it must not cry wolf while the base stands.
    A base-drift check that fires on a valid derivation would be routed around
    inside a week.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "STOP_BASE_ATR = 2.5\nSTOP_RANGE_SCALE = 0.9\n",
        """
        numbers:
          - id: src.risk.rules.STOP_BASE_ATR
            value: 2.5
            status: arbitrary
            note: the base
            open_question: what stop width does this desk's own MAE support?
            cost_while_unanswered: every unbacked stop scales off it
          - id: src.risk.rules.STOP_RANGE_SCALE
            value: 0.9
            status: derived
            derived_from: src.risk.rules.STOP_BASE_ATR
            base_value: 2.5
        """,
    )
    assert not _kinds(root, ledger)


def test_a_renamed_or_deleted_number_leaves_a_detectable_orphan() -> None:
    """A stale entry would let coverage look complete while the code moved."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "# the constant was deleted\n",
        """
        numbers:
          - id: src.risk.rules.MAX_HEAT_PCT
            value: 4.2
            status: arbitrary
        """
        + _DEBT,
    )
    assert "orphan" in _kinds(root, ledger)


def test_zero_is_not_a_site_but_one_is() -> None:
    """0 is an empty default and the bottom of an ordinal scale. 1 is not the
    identity when it is an ATR multiple, and excluding it hid the hard floor
    under every stop this desk sets.
    """
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "NEUTRAL_ZERO = 0.0\nABSOLUTE_MIN_STOP_ATR = 1.0\n",
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert [p.site_id for p in problems] == [
        "src.risk.rules.ABSOLUTE_MIN_STOP_ATR"
    ]


def test_a_citation_pointing_at_nothing_is_reported() -> None:
    """Rule 7, firing. `broken_citations` is checked directly rather than
    through a fixture tree, because a fixture has no `docs/` and every real
    citation would read as missing there — a test that passes for the wrong
    reason is worse than no test.
    """
    root = Path(__file__).resolve().parent.parent
    invented = {
        "src.risk.rules.MAX_HEAT_PCT": {
            "status": "sourced",
            "source": "the measured matrix in docs/DOES_NOT_EXIST.md:12",
        },
        "src.risk.rules.PAST_EOF": {
            "status": "sourced",
            "source": "see src/number_sources.py:999999",
        },
        "src.risk.rules.FINE": {
            "status": "sourced",
            "source": "see src/number_sources.py:1",
        },
    }
    reported = {site_id for site_id, _, _ in broken_citations(invented, root)}
    assert reported == {"src.risk.rules.MAX_HEAT_PCT", "src.risk.rules.PAST_EOF"}


def test_a_default_pointed_at_an_unscoped_module_does_not_vanish() -> None:
    """The evasion in miniature. `Config.floor = SOME_NAME` imported from a
    module nobody scoped used to remove the number from the gate entirely.
    """
    root = Path(pytest.importorskip("tempfile").mkdtemp())
    root, ledger = _fixture(
        root,
        "from src.hidden import HIDDEN_FLOOR\n\n\n"
        "class RulesConfig:\n    floor: float = HIDDEN_FLOOR\n",
        "numbers: []\n",
    )
    (root / "src" / "hidden.py").write_text("HIDDEN_FLOOR = 2.75\n")
    problems = audit(repo_root=root, ledger_path=ledger)
    assert [(p.kind, p.site_id) for p in problems] == [
        ("unsourced", "src.risk.rules.RulesConfig.floor")
    ]


# --------------------------------------------------------------------------
# Rules (c), (d), (e): the shapes (a)/(b) could not see. 2026-09-19.
# --------------------------------------------------------------------------


def test_the_named_hidden_trade_numbers_are_now_sites() -> None:
    """Every number the 2026-09-19 brief named as invisible, by the shape it
    hid in: two parameter defaults, a class attribute, and board item 138's
    inline order-price buffers.
    """
    ids = {site.site_id for site in collect_sites()}
    # (c) parameter defaults.
    assert "src.pipeline.TradingPipeline._clamp_queued_earnings_buys(max_pct)" in ids
    assert "src.risk.rules.RiskRuleEngine.check(max_correlated_cluster_pct)" in ids
    # (d) class attributes.
    assert "src.execution.broker.AlpacaBroker.STOP_LIMIT_BUFFER_PCT" in ids
    assert "src.pipeline.TradingPipeline._EMERGENCY_LIMIT_CUSHION_PCT" in ids
    # (e) item 138: the 1% de-lever ladder, the 0.5% exit offsets.
    assert "src.pipeline.TradingPipeline._enforce_gross_ceiling:factor[1]" in ids
    assert "src.pipeline.TradingPipeline._force_delever:factor[1]" in ids
    assert "src.pipeline_stages.ExecutionStage._run_session:factor[0]" in ids
    assert "src.pipeline.TradingPipeline._midday_execute_llm_actions:factor[3]" in ids


def test_a_parameter_default_is_a_site() -> None:
    """Rule (c). A cap passed nowhere and defaulted in a signature is as live
    as a module constant, and was invisible."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        def clamp(decisions, *, max_pct: float = 5.0, floor=0.0):
            return decisions


        class Engine:
            def check(self, cluster_pct: float = 50.0):
                return cluster_pct
        """,
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert sorted(p.site_id for p in problems if p.kind == "unsourced") == [
        "src.risk.rules.Engine.check(cluster_pct)",
        "src.risk.rules.clamp(max_pct)",
    ]


def test_an_attribute_on_any_class_is_a_site() -> None:
    """Rule (d). The 3% stop-limit buffer is an attribute of the broker class,
    not a `*Config` field, and no rule saw it."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        class Broker:
            STOP_LIMIT_BUFFER_PCT = 0.03
            retries: int = 0

            class Inner:
                hops = 8
        """,
        "numbers: []\n",
    )
    problems = audit(repo_root=root, ledger_path=ledger)
    assert sorted(p.site_id for p in problems if p.kind == "unsourced") == [
        "src.risk.rules.Broker.Inner.hops",
        "src.risk.rules.Broker.STOP_LIMIT_BUFFER_PCT",
    ]


def test_a_near_one_price_multiplier_is_a_site_and_unit_arithmetic_is_not() -> None:
    """Rule (e), both halves. The margins fire -- including both arms of a
    conditional and a folded `1 - 0.03` -- and the unit conversions, the
    sign flip, the epsilon and constant arithmetic do not."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        """
        def exit_limit(price, is_cover, bps, deficit, qty):
            sell = round(price * 0.995, 2)
            ladder = price * (1.01 if is_cover else 0.99)
            stop = price * (1 - 0.03)
            cushion = deficit * 1.02
            skip = price / 1.02
            cap = price * (1 + bps / 10_000.0)
            flipped = qty * -1.0
            days = 365 * 5
            eps = qty + 1e-9
            pct = qty / price * 100.0
            half = qty / 2
            return sell, ladder, stop, cushion, skip, cap, flipped, days, eps, pct, half
        """,
        "numbers: []\n",
    )
    found = {
        p.site_id: p.detail
        for p in audit(repo_root=root, ledger_path=ledger)
        if p.kind == "unsourced"
    }
    assert sorted(found) == [
        f"src.risk.rules.exit_limit:factor[{n}]" for n in range(6)
    ]
    assert "0.995" in found["src.risk.rules.exit_limit:factor[0]"]
    assert "0.97" in found["src.risk.rules.exit_limit:factor[3]"]


def test_the_new_shapes_do_not_leak_into_the_unscoped_sentinel() -> None:
    """The sentinel's count is defined as module-level constants. Rules
    (c)-(e) outside scope would have moved it by hundreds for no reason and
    turned its ceiling into noise."""
    root, ledger = _fixture(
        Path(pytest.importorskip("tempfile").mkdtemp()),
        "X = 1\n",
        "numbers: []\n",
    )
    (root / "src" / "unscoped.py").write_text(
        "def f(a=5.0):\n    return a * 0.99\n\n\nclass K:\n    B = 3\n"
    )
    assert collect_unscoped_sites(root) == []
