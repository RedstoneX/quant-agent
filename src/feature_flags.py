"""Fail the build when a boolean feature switch can hide.

THE DEFECT THIS CLOSES. `SmartMoneyConfig.congress_enabled` was added
2026-09-04 (#271), shipped `False` "off by default, opt in after review",
and was never set `True` in `config/settings.yaml` at any commit since. That
would be an ordinary, correctly-labelled off switch except that three
owner-facing surfaces kept describing it as running: the Telegram smart-money
label (`src/notifier.py`, `"the insider-and-congressional-trading feed"`),
the pre-market refresh log line (`src/pipeline.py`,
`"Smart-money refresh (SEC Form 4 + congressional)"`), and
`docs/qamc_trading_desk_workflow.html`. Nobody noticed for five weeks because
nothing checked that a switch's declared state matched its real one, or that
a switch existed at all outside its own field definition.

WHAT THIS MODULE DOES. It enumerates every boolean field on every
`src.config.*Config` class (the settings models `AppConfig` is built from),
resolves its EFFECTIVE value the same way `src.config.load_config` does —
`config/settings.yaml`'s section value if the key is present there,
otherwise the pydantic field default — and requires each one to have an
entry in `config/feature_flags.yaml` recording that effective value, whether
it is intentional, and why. See `tests/test_feature_flags.py`, which is what
actually fails `pytest` (the check branch protection requires).

WHAT IT DOES NOT DO. Exactly the caveat `src/number_sources.py` states for
its own ledger, and for the same reason: this gate tests that a declaration
EXISTS and matches the live effective value. It cannot test that the
`reason` field is TRUE, only that one was written down instead of invented
on request — the task that built this file explicitly forbids inventing a
reason, and `config/feature_flags.yaml` says "reason not recorded" wherever
git history and the code did not already say why.

SCOPE. Every class in `src/config.py` whose name ends in `Config` (matching
`src/number_sources.py`'s own suffix rule, which the docstring there argues
makes a new `FooConfig` covered the day it is written) except `AppConfig`
itself, which composes the others and defines no switches of its own. A
field is in scope when its type annotation is exactly `bool` — `bool | None`
tri-state fields are a different shape (absent/true/false) and none exist in
this file today; if one is added, `collect_switches` will not see it and
`MAX_UNDECLARED_TRISTATE_FIELDS` below exists so that silence cannot pass
uninspected.

EFFECTIVE VALUE. `src.config.load_config` builds `AppConfig(**raw)` from
`config/settings.yaml`'s parsed dict with no boolean-specific transform
between the YAML and the pydantic default (see that function and
`_appconfig_sections` below) — no environment variable overrides a boolean
switch. So the effective value is: the YAML section's key if present, else
the field's own default. A field with NO default (`paper`, `require_stop_loss`)
must be present in `config/settings.yaml` or `AppConfig` fails to construct
at all; this module treats a missing required key as a load error, not as a
silent effective value, exactly as production behaves.

FOUR THINGS THE GATE IS CHECKED FOR:

  1. COVERAGE — every boolean switch found by the scan has a declaration.
     A new switch with no entry fails the build.
  2. LIVE MATCH — the declaration's `effective_value` equals what
     `config/settings.yaml` layered over the model default actually
     produces today. A switch flipped in either file without updating the
     declaration fails the build — this is the exact shape of bug the
     congressional feature was: the file said `False` and stayed right,
     but nothing would have caught it going stale in either direction.
  3. ORPHANS — a declaration naming a switch that no longer exists (renamed,
     removed, or its class no longer ends in `Config`) fails the build.
  4. COMPLETENESS OF FIELDS — every declaration carries `intentional` (a
     bool) and `reason` (non-empty text). Where nothing in git history or
     the code's own comments says why, the required text is literally
     "reason not recorded" — that is itself the finding, not a placeholder
     to fill with a guess.

WHAT THIS STRUCTURALLY CANNOT CATCH:

  * A WRONG reason. Nothing here reads `reason` and checks it is true, only
    that something non-empty was written instead of invented to satisfy the
    schema.
  * A feature switch expressed some OTHER way — an environment variable, a
    CLI flag, a magic string default instead of a `bool` field, or a switch
    inside a nested non-`Config`-suffixed model. This gate only sees
    `src/config.py`'s own `*Config` boolean fields.
  * Owner-facing TEXT (a Telegram label, a log line, a workflow page)
    describing a switch as running when it is not. That mismatch is exactly
    what let the congressional feature hide, and it is a separate, harder
    problem: this gate can tell you a switch is off, but nothing here reads
    prose and checks it agrees with the switch. That remains a manual
    documentation pass, same as `docs/qamc_trading_desk_workflow.html` needed
    here.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The declaration file. Data, not code — mirrors `config/number_ledger.yaml`.
DECLARATIONS_PATH = REPO_ROOT / "config" / "feature_flags.yaml"

#: Deployed overrides — the file `load_config` actually reads.
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

#: The single module every switch lives in today.
CONFIG_MODULE = REPO_ROOT / "src" / "config.py"

#: Sentinel for the one thing the suffix/annotation scan structurally cannot
#: see: a `bool | None` tri-state switch (absent/true/false, a different
#: shape than a plain bool). None exist today — measured by this same scan,
#: see `tests/test_feature_flags.py`. If one appears, this constant must be
#: raised in the same commit, as a reviewed line saying it was looked at.
MAX_UNDECLARED_TRISTATE_FIELDS = 0


@dataclass(frozen=True)
class FlagSite:
    """One boolean switch: a field on a `*Config` class in src/config.py."""

    flag_id: str
    class_name: str
    field: str
    lineno: int
    code_default: bool | None  # None means the field has no default (required)

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.flag_id} (default={self.code_default!r}) @ src/config.py:{self.lineno}"


@dataclass
class FlagProblem:
    kind: str
    flag_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"[{self.kind}] {self.flag_id}: {self.detail}"


def _bool_default(node: ast.AST | None) -> tuple[bool, bool | None]:
    """`(has_default, value)` for a field's assigned default node.

    Handles a bare `True`/`False` literal and `Field(default=True/False, ...)`.
    Anything else (a name, a call with no literal `default=`) is reported as
    "has a default but it is not a literal we can resolve" by returning
    `(True, None)` — distinct from "no default at all", `(False, None)`.
    """
    if node is None:
        return False, None
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return True, node.value
    if isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant) and isinstance(
                kw.value.value, bool
            ):
                return True, kw.value.value
        return True, None
    return True, None


def _is_bool_annotation(annotation: ast.AST) -> bool:
    return isinstance(annotation, ast.Name) and annotation.id == "bool"


def _is_tristate_bool_annotation(annotation: ast.AST) -> bool:
    """`bool | None`, either spelling (`X | None` or `Optional[X]`)."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        names = []
        for side in (annotation.left, annotation.right):
            if isinstance(side, ast.Name):
                names.append(side.id)
            elif isinstance(side, ast.Constant) and side.value is None:
                names.append("None")
        return set(names) == {"bool", "None"}
    if isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name):
        if annotation.value.id == "Optional":
            inner = annotation.slice
            return isinstance(inner, ast.Name) and inner.id == "bool"
    return False


def collect_switches(config_module: Path | None = None) -> list[FlagSite]:
    """Every boolean field on a `*Config` class in `src/config.py`."""
    path = config_module or CONFIG_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: list[FlagSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.endswith("Config") or node.name == "AppConfig":
            continue
        for body_node in node.body:
            if isinstance(body_node, ast.AnnAssign) and isinstance(body_node.target, ast.Name):
                if not _is_bool_annotation(body_node.annotation):
                    continue
                has_default, default_value = _bool_default(body_node.value)
                code_default = default_value if has_default else None
                sites.append(
                    FlagSite(
                        flag_id=f"src.config.{node.name}.{body_node.target.id}",
                        class_name=node.name,
                        field=body_node.target.id,
                        lineno=body_node.lineno,
                        code_default=code_default,
                    )
                )
    return sorted(sites, key=lambda s: s.flag_id)


def collect_tristate_switches(config_module: Path | None = None) -> list[str]:
    """Every `bool | None` field on a `*Config` class — the shape this
    module's plain-bool scan cannot see. Used only by the sentinel test."""
    path = config_module or CONFIG_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.endswith("Config") or node.name == "AppConfig":
            continue
        for body_node in node.body:
            if isinstance(body_node, ast.AnnAssign) and isinstance(body_node.target, ast.Name):
                if _is_tristate_bool_annotation(body_node.annotation):
                    out.append(f"src.config.{node.name}.{body_node.target.id}")
    return sorted(out)


def _appconfig_sections(root: Path | None = None) -> dict[str, str]:
    """`{ConfigClassName: settings.yaml section}`, read from `AppConfig`.

    Same technique as `src.number_sources._appconfig_sections`: read off
    `AppConfig`'s own field annotations so a renamed section cannot
    desynchronise this check from the loader it is checking.
    """
    base = root or REPO_ROOT
    tree = ast.parse((base / "src" / "config.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "AppConfig":
            continue
        out: dict[str, str] = {}
        for body_node in node.body:
            if (
                isinstance(body_node, ast.AnnAssign)
                and isinstance(body_node.annotation, ast.Name)
                and isinstance(body_node.target, ast.Name)
            ):
                out[body_node.annotation.id] = body_node.target.id
        return out
    return {}


def effective_values(root: Path | None = None) -> dict[str, bool]:
    """`{flag_id: effective bool}` — `config/settings.yaml` layered over the
    model default, exactly the way `src.config.load_config` builds `AppConfig`
    (`AppConfig(**raw)`, no boolean-specific transform in between).

    A switch whose field has no code default (`paper`, `require_stop_loss`)
    but is also absent from `config/settings.yaml` cannot be resolved here —
    that is a load error in production too (`AppConfig(**raw)` raises), not a
    silent effective value, so it is reported as a `missing-required` problem
    by the caller rather than defaulted to anything.
    """
    base = root or REPO_ROOT
    settings = base / "config" / "settings.yaml"
    raw = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    sections = _appconfig_sections(base)
    class_to_section = sections  # {class_name: section_name}
    out: dict[str, bool] = {}
    for site in collect_switches(base / "src" / "config.py"):
        section = class_to_section.get(site.class_name)
        block = raw.get(section) if section else None
        if isinstance(block, dict) and site.field in block and isinstance(
            block[site.field], bool
        ):
            out[site.flag_id] = block[site.field]
        elif site.code_default is not None:
            out[site.flag_id] = site.code_default
        # else: unresolved — required field missing from settings.yaml.
        # Left out of the map; the caller treats that as its own problem.
    return out


def load_declarations(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The declaration file as `{flag_id: entry}`. Raises on a duplicate id."""
    decl_path = path or DECLARATIONS_PATH
    raw = yaml.safe_load(decl_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("flags") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        flag_id = entry.get("id")
        if not flag_id:
            raise ValueError(f"declaration with no id: {entry!r}")
        if flag_id in out:
            raise ValueError(f"duplicate declaration id: {flag_id}")
        out[flag_id] = entry
    return out


def audit(root: Path | None = None) -> list[FlagProblem]:
    """Every reason `tests/test_feature_flags.py` should fail the build."""
    base = root or REPO_ROOT
    sites = collect_switches(base / "src" / "config.py")
    by_id = {s.flag_id: s for s in sites}
    declarations = load_declarations(base / "config" / "feature_flags.yaml")
    settings = base / "config" / "settings.yaml"
    raw_settings = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    sections = _appconfig_sections(base)
    effective = effective_values(base)

    problems: list[FlagProblem] = []

    for site in sites:
        section = sections.get(site.class_name)
        block = raw_settings.get(section) if section else None
        deployed_present = isinstance(block, dict) and site.field in block
        if site.code_default is None and not deployed_present:
            problems.append(
                FlagProblem(
                    "missing-required",
                    site.flag_id,
                    "has no code default and config/settings.yaml does not set "
                    "it either — AppConfig(**raw) would raise in production, "
                    "so there is no effective value to declare.",
                )
            )
            continue

        entry = declarations.get(site.flag_id)
        if entry is None:
            problems.append(
                FlagProblem(
                    "undeclared",
                    site.flag_id,
                    f"boolean switch at src/config.py:{site.lineno} with no "
                    f"entry in config/feature_flags.yaml. Record its effective "
                    f"value, whether it is intentional, and why.",
                )
            )
            continue

        live_value = effective.get(site.flag_id)
        recorded_value = entry.get("effective_value")
        if not isinstance(recorded_value, bool) or recorded_value != live_value:
            problems.append(
                FlagProblem(
                    "value-drift",
                    site.flag_id,
                    f"live effective value is {live_value!r} (settings.yaml "
                    f"layered over the code default) but the declaration "
                    f"records {recorded_value!r}.",
                )
            )

        if not isinstance(entry.get("intentional"), bool):
            problems.append(
                FlagProblem(
                    "no-intentional",
                    site.flag_id,
                    "requires `intentional:` (true/false) — was this value "
                    "chosen, or just never revisited?",
                )
            )

        if not str(entry.get("reason") or "").strip():
            problems.append(
                FlagProblem(
                    "no-reason",
                    site.flag_id,
                    "requires `reason:`. If git history and the code's own "
                    "comments give none, the required text is literally "
                    "'reason not recorded' — that is the finding, not "
                    "something to invent.",
                )
            )

    for flag_id in declarations:
        if flag_id not in by_id:
            problems.append(
                FlagProblem(
                    "orphan",
                    flag_id,
                    "declaration no longer matches any boolean field — the "
                    "switch was renamed, removed, or its class no longer ends "
                    "in Config. Remove the entry or point it at the new site.",
                )
            )

    return problems
