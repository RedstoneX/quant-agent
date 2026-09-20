"""The gate: a boolean feature switch cannot exist, or drift, unseen.

Built after an audit found `SmartMoneyConfig.congress_enabled` had been off
since it shipped (2026-09-04, #271) while three owner-facing surfaces
described it as running. Read `src/feature_flags.py`'s module docstring for
the scope rule, the effective-value resolution, and what this cannot catch —
in particular, it cannot check that owner-facing TEXT (a Telegram label, a
log line, a workflow page) agrees with a switch's real state.

Same structure as `tests/test_number_sources.py`: the first group is the gate
against the live tree, the second proves each failure mode actually fires
against a synthetic fixture, because an untested gate is indistinguishable
from one that passes everything.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.feature_flags import (
    MAX_UNDECLARED_TRISTATE_FIELDS,
    audit,
    collect_switches,
    collect_tristate_switches,
    effective_values,
    load_declarations,
)

# --------------------------------------------------------------------------
# The gate, against the live tree.
# --------------------------------------------------------------------------


def test_every_boolean_switch_is_declared_and_current() -> None:
    """THE GATE. Every boolean field on every `src.config.*Config` class has
    a declaration in `config/feature_flags.yaml`, that declaration's
    `effective_value` matches what `config/settings.yaml` layered over the
    code default actually produces, and no declaration is orphaned.

    If this fails on your branch you added, removed, or flipped a feature
    switch without updating `config/feature_flags.yaml`. Say what changed and
    why — `reason not recorded` is honest only when nothing else says why;
    it is not a way to skip explaining a change you just made.
    """
    problems = audit()
    assert not problems, "\n".join(
        ["undeclared, stale or orphaned feature switches:", ""]
        + [f"  {p}" for p in problems]
    )


def test_congress_enabled_is_declared_on_and_the_reason_is_on_record() -> None:
    """The specific regression this file exists to prevent. Switched on
    2026-09-20 per owner ruling 2026-09-19 (docs/INCIDENT_HISTORY.md, that
    date): congressional evidence must be weighted by the PM, never zeroed
    out. If this switch is ever flipped again in `config/settings.yaml`
    without this declaration being updated,
    `test_every_boolean_switch_is_declared_and_current` above catches the
    drift; this test additionally pins today's known-good state so a change
    here is never silent even if someone edits the declaration file by hand
    to match a change to settings.yaml.
    """
    declarations = load_declarations()
    entry = declarations["src.config.SmartMoneyConfig.congress_enabled"]
    assert entry["effective_value"] is True
    live = effective_values()
    assert live["src.config.SmartMoneyConfig.congress_enabled"] is True


def test_every_declared_switch_carries_a_reason_and_an_intentional_flag() -> None:
    """Schema completeness on the live file, independent of `audit()`'s own
    per-field checks — a declaration missing `reason` or `intentional`
    should never reach main even if some other bug in `audit()` let it pass.
    """
    for flag_id, entry in load_declarations().items():
        assert isinstance(entry.get("intentional"), bool), flag_id
        assert str(entry.get("reason") or "").strip(), flag_id


def test_no_tristate_boolean_switch_is_hiding_from_the_scan() -> None:
    """The one shape this module's plain-`bool` scan cannot see: a
    `bool | None` tri-state field. None exist today. If one is added, this
    fails until `MAX_UNDECLARED_TRISTATE_FIELDS` is raised as a reviewed line
    saying it was looked at — mirrors `MAX_UNSCOPED_NUMERIC_SITES` in
    `src/number_sources.py`.
    """
    assert len(collect_tristate_switches()) <= MAX_UNDECLARED_TRISTATE_FIELDS


# --------------------------------------------------------------------------
# Proof each failure mode fires, against synthetic fixtures.
# --------------------------------------------------------------------------


def _fixture(tmp_path: Path, config_py: str, settings_yaml: str, declarations_yaml: str) -> Path:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "config.py").write_text(textwrap.dedent(config_py))
    (tmp_path / "config" / "settings.yaml").write_text(textwrap.dedent(settings_yaml))
    (tmp_path / "config" / "feature_flags.yaml").write_text(
        textwrap.dedent(declarations_yaml)
    )
    return tmp_path


_MINIMAL_CONFIG_PY = """
class WidgetConfig:
    enabled: bool = False


class AppConfig:
    widget: WidgetConfig
"""


def _kinds(root: Path) -> set[str]:
    return {p.kind for p in audit(root)}


def test_an_undeclared_switch_fails(tmp_path: Path) -> None:
    """The core coverage rule: a new boolean switch with no declaration."""
    root = _fixture(tmp_path, _MINIMAL_CONFIG_PY, "widget:\n  enabled: false\n", "flags: []\n")
    assert "undeclared" in _kinds(root)


def test_a_declared_switch_matching_the_live_value_passes(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,
        "widget:\n  enabled: false\n",
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false
            code_default: false
            intentional: true
            reason: shipped dark, no owner-facing surface claims it runs.
            decided: null
            source: null
        """,
    )
    assert not _kinds(root)


def test_a_declaration_that_disagrees_with_settings_yaml_fails(tmp_path: Path) -> None:
    """THE EXACT SHAPE of the incident this file was built for: the switch's
    real, deployed state disagrees with what is written down about it.
    """
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,
        "widget:\n  enabled: true\n",  # deployed ON
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false   # declaration says OFF
            code_default: false
            intentional: true
            reason: shipped dark.
            decided: null
            source: null
        """,
    )
    assert "value-drift" in _kinds(root)


def test_a_declaration_that_disagrees_with_the_code_default_fails(tmp_path: Path) -> None:
    """Same drift, the other direction: no settings.yaml override, but the
    code default itself changed since the declaration was written.
    """
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,  # code default False
        "widget: {}\n",  # nothing set — resolves to the code default
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: true   # stale — code default is actually False
            code_default: true
            intentional: true
            reason: stale entry.
            decided: null
            source: null
        """,
    )
    assert "value-drift" in _kinds(root)


def test_a_declaration_missing_intentional_fails(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,
        "widget:\n  enabled: false\n",
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false
            code_default: false
            reason: no intentional flag given.
        """,
    )
    assert "no-intentional" in _kinds(root)


def test_a_declaration_with_no_reason_fails(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,
        "widget:\n  enabled: false\n",
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false
            code_default: false
            intentional: true
        """,
    )
    assert "no-reason" in _kinds(root)


def test_reason_not_recorded_is_accepted_as_the_honest_answer(tmp_path: Path) -> None:
    """The one thing this gate must NOT do is punish honesty: "reason not
    recorded" is exactly what the task that built this file requires when
    git history gives nothing, and it must satisfy the schema.
    """
    root = _fixture(
        tmp_path,
        _MINIMAL_CONFIG_PY,
        "widget:\n  enabled: false\n",
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false
            code_default: false
            intentional: false
            reason: "reason not recorded"
            decided: null
            source: null
        """,
    )
    assert not _kinds(root)


def test_a_renamed_or_removed_switch_leaves_a_detectable_orphan(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        "class WidgetConfig:\n    pass\n\n\nclass AppConfig:\n    widget: WidgetConfig\n",
        "widget: {}\n",
        """
        flags:
          - id: src.config.WidgetConfig.enabled
            effective_value: false
            code_default: false
            intentional: true
            reason: was here once.
            decided: null
            source: null
        """,
    )
    assert "orphan" in _kinds(root)


def test_a_required_field_absent_from_settings_yaml_is_reported(tmp_path: Path) -> None:
    """A field with no code default (like `paper`, `require_stop_loss`) must
    be set in `config/settings.yaml` or `AppConfig(**raw)` fails to build in
    production. Missing here is a `missing-required` problem, not a silently
    resolved effective value.
    """
    config_py = """
    class WidgetConfig:
        enabled: bool


    class AppConfig:
        widget: WidgetConfig
    """
    root = _fixture(tmp_path, config_py, "widget: {}\n", "flags: []\n")
    assert "missing-required" in _kinds(root)


def test_a_non_bool_field_is_not_a_site(tmp_path: Path) -> None:
    """Only `bool` fields are in scope. A `str`/`int`/`float` field, however
    flag-shaped its name, is a different check's job — this file is only
    about switches, not about every settings model field."""
    config_py = """
    class WidgetConfig:
        name: str = "widget"


    class AppConfig:
        widget: WidgetConfig
    """
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "config.py").write_text(textwrap.dedent(config_py))
    ids = {s.flag_id for s in collect_switches(tmp_path / "src" / "config.py")}
    assert not ids


def test_appconfig_itself_defines_no_switches(tmp_path: Path) -> None:
    """`AppConfig` composes the other classes; it is excluded by name so a
    boolean someday added directly to it does not silently vanish from the
    scan by being miscategorised — it should be added to `SCOPED`-style
    exclusion deliberately, not by accident of the suffix rule."""
    ids = {s.class_name for s in collect_switches()}
    assert "AppConfig" not in ids
