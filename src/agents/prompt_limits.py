"""Render a standing instruction sheet's risk limits from the live config.

THE DEFECT THIS CLOSES. The AI Risk Manager's standing sheet
(`config/prompts/risk_manager.md`) stated the desk's limits as hand-typed
prose. Nothing tied that prose to `config/settings.yaml`, which is what the
deterministic engine actually enforces, so the two drifted — provably, twice:

  * the sheet told the reviewer the long single-name ceiling was 33% while
    `risk.max_position_pct` had been 65 since 2026-09-11 (owner override).
    Every plan for two days was audited against a ceiling less than half the
    real one.
  * the same sheet told the seat a 1.5 reward:risk floor was "the number to
    enforce" months after the floor stopped being a gate — the named cause of
    three of the four whole-plan vetoes in the archived database (fixed
    separately, PR #341).

The pattern is not "someone forgot to update the prompt". It is that a limit
had TWO homes and only one of them was mechanically enforced. This module
gives it one: the sheet carries `{{risk.<field>}}` placeholders, and the
value is read at run time off the same `RiskConfig` the engine is built from.

FAIL LOUD, NEVER BLANK. A placeholder naming a setting that does not exist
raises `PromptPlaceholderError` rather than rendering an empty string or a
stale default. A risk reviewer briefed with "the single-name ceiling is %"
is worse than one briefed with a wrong number, because nothing downstream
can see that it happened. The pipeline builds the agent at startup, so the
failure surfaces before any capital is at risk.

Scope is deliberately narrow: numeric fields on `src.config.RiskConfig` (and
a short allowlist of its COMPUTED properties, which are derivations of those
fields rather than independent numbers). No other namespace resolves. This
module holds no copy of any limit's value — only the rules for reading one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: `{{risk.max_position_pct}}` — one namespace, one dotted field, no
#: expressions. Whitespace inside the braces is tolerated so a wrapped line
#: in the sheet does not silently stop substituting.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*\}\}")

#: The only namespace that resolves today. A second seat's sheet would add
#: its own here rather than reaching into arbitrary attributes.
RISK_NAMESPACE = "risk"

#: Computed properties of `RiskConfig` that are legitimate placeholder
#: targets. Each is a DERIVATION of the model's own fields (see
#: `src/config.py`), not an independently chosen number, so rendering one is
#: still rendering the single source of truth. Anything not a declared field
#: and not on this list is refused, so a typo cannot silently reach a method.
RISK_COMPUTED_PROPERTIES = frozenset({
    "effective_max_daily_loss_pct",
    "drawdown_5d_threshold_pct",
    "drawdown_20d_threshold_pct",
    "sector_hard_ceiling_pct",
})


class PromptPlaceholderError(RuntimeError):
    """A prompt placeholder names something the live config does not have.

    Deliberately a hard error. The alternative — rendering blank, or a
    default typed into this file — reinstates exactly the second home for a
    number that this module exists to remove.
    """


def _format_number(value: float) -> str:
    """Render a limit the way an operator writes it: `65`, not `65.0`.

    `float` is what pydantic gives back for every one of these fields even
    when settings.yaml wrote an integer, and `max_position_pct=65.0` in a
    prompt reads as spurious precision next to `short_gap_risk_multiple=1.5`
    where the decimal is real.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a limit
        raise PromptPlaceholderError(
            "placeholder resolved to a boolean, which is not a numeric limit",
        )
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    # Trim float noise (1.3416407865 -> 1.34 is already done in config; this
    # only guards a future derived property that isn't pre-rounded).
    return f"{as_float:g}"


def resolve_placeholder(key: str, risk_config: Any) -> str:
    """Resolve one dotted placeholder key against the live config.

    Raises `PromptPlaceholderError` for an unknown namespace, an unknown
    field, or a value that is not a number — never returns a fallback.
    """
    namespace, _, field = key.partition(".")
    if namespace != RISK_NAMESPACE:
        raise PromptPlaceholderError(
            f"unknown placeholder namespace {namespace!r} in {{{{{key}}}}} — "
            f"only {RISK_NAMESPACE!r} resolves",
        )
    declared = set(getattr(type(risk_config), "model_fields", {}) or {})
    if field not in declared and field not in RISK_COMPUTED_PROPERTIES:
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} names no setting: {field!r} is not a "
            f"field of {type(risk_config).__name__} and is not one of the "
            f"allowed computed properties "
            f"({', '.join(sorted(RISK_COMPUTED_PROPERTIES))}). "
            f"Add the setting to config/settings.yaml — do not type the "
            f"number into the prompt.",
        )
    try:
        value = getattr(risk_config, field)
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} could not be read from the live "
            f"config: {exc}",
        ) from exc
    if value is None:
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} resolved to None. An unset optional "
            f"setting must be rendered through its derived property (e.g. "
            f"`effective_max_daily_loss_pct`), not left blank in the prompt.",
        )
    if not isinstance(value, (int, float)):
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} resolved to {type(value).__name__}, "
            f"not a number — only numeric limits may be rendered",
        )
    return _format_number(value)


def render_prompt_limits(text: str, risk_config: Any) -> str:
    """Substitute every `{{risk.*}}` placeholder in `text` with its live value.

    Every placeholder must resolve; the first that does not raises. There is
    no partial render, because a sheet that is half live and half stale is
    the condition this replaces.
    """
    def _sub(match: re.Match[str]) -> str:
        return resolve_placeholder(match.group(1), risk_config)

    return PLACEHOLDER_RE.sub(_sub, text)


def placeholders_in(text: str) -> set[str]:
    """Every dotted key the text asks to have substituted."""
    return {m.group(1) for m in PLACEHOLDER_RE.finditer(text)}


def load_risk_config_from_settings(path: str | Path):
    """Parse the `risk:` block of a settings file into `src.config.RiskConfig`.

    Used only as the fallback for a `RiskManagerAgent` constructed without an
    explicit config (every test fixture and script that predates this change).
    It reads the SAME file the pipeline reads, so it is not a second copy of
    the settings — it is a second read of the one copy.
    """
    import yaml

    from src.config import RiskConfig, _walk_and_substitute

    with open(path) as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict) or "risk" not in raw:
        raise PromptPlaceholderError(
            f"{path} has no `risk:` block — the risk manager's standing sheet "
            f"cannot be rendered without the settings the engine enforces",
        )
    return RiskConfig(**_walk_and_substitute(raw["risk"]))
