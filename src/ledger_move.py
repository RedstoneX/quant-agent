"""Mechanical migration of number-ledger ids and `SCOPED_PATHS` across a module split.

Why this exists (board item 210, step 0 of `docs/PIPELINE_SPLIT_PLAN.md` §5.2):
the number ledger keys every row by a site id of the shape
``<module>.<Qual>.<func>(<param>)`` / ``<module>.<CONST>`` / ``...<func>:factor[N]``,
and it scopes `src/pipeline.py` and `src/pipeline_stages.py` explicitly through
`src.number_sources.SCOPED_PATHS`. When a method moves to a new module every
ledger id naming the old module becomes a lie, and if the new file is not added
to `SCOPED_PATHS` its numbers drop out of the guard SILENTLY. This module makes
that rewrite mechanical and, more importantly, CHECKABLE: `plan_move` says what
would change, `apply_move` performs exactly that, and `verify_move` re-reads the
result and fails if anything was missed, duplicated or left behind.

It moves no code and changes no number. Only ids, site paths and the scope list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A ledger entry's id line, e.g. ``  - id: src.pipeline.TradingPipeline._foo(n)``.
_ID_LINE = re.compile(r"^(?P<indent>\s*)-\s+id:\s*(?P<id>\S+)\s*$")
#: The ``site:`` line inside the same entry.
_SITE_LINE = re.compile(r"^(?P<indent>\s*)site:\s*(?P<site>\S+)\s*$")


def module_to_path(module: str) -> str:
    """``src.pipeline_exits`` -> ``src/pipeline_exits.py`` (the `site:` spelling)."""
    return module.replace(".", "/") + ".py"


@dataclass(frozen=True)
class MoveSpec:
    """One module-to-module move of a named set of methods and/or module constants.

    `names` are bare names as they appear in the ledger id -- a method name
    (``_force_delever``), or a module-level constant (``_PM_PROFILE_SYMBOL_CAP``).
    `old_qual`/`new_qual` are the owning class names; a method that moves into a
    mixin changes BOTH its module and its qualname, and a rename of the class is
    the normal case for this plan (``TradingPipeline`` -> ``DeleverMixin``).
    Pass `new_qual=None` with `old_qual=None` for module-level constants.
    """

    old_module: str
    new_module: str
    names: frozenset[str]
    old_qual: str | None = None
    new_qual: str | None = None

    def __post_init__(self) -> None:
        if not self.names:
            raise ValueError("MoveSpec needs at least one name to move")
        if self.old_module == self.new_module:
            raise ValueError("MoveSpec old_module and new_module are the same")
        if (self.old_qual is None) != (self.new_qual is None):
            raise ValueError("pass both old_qual and new_qual, or neither")

    @property
    def old_prefix(self) -> str:
        return f"{self.old_module}.{self.old_qual}." if self.old_qual else f"{self.old_module}."

    @property
    def new_prefix(self) -> str:
        return f"{self.new_module}.{self.new_qual}." if self.new_qual else f"{self.new_module}."

    def rewrite_id(self, identifier: str) -> str | None:
        """The new id for `identifier`, or None when this spec does not touch it."""
        if not identifier.startswith(self.old_prefix):
            return None
        tail = identifier[len(self.old_prefix) :]
        # The bare name runs up to the first `(` (a parameter default site) or
        # `:` (a `:factor[N]` inline-operand site); anything else is a further
        # qualname segment and is not a name this spec names.
        name = re.split(r"[(:]", tail, maxsplit=1)[0]
        if name not in self.names:
            return None
        return self.new_prefix + tail


@dataclass
class MovePlan:
    """What a move would change. Produced before anything is written."""

    spec: MoveSpec
    id_rewrites: dict[str, str] = field(default_factory=dict)
    scoped_path_added: str | None = None
    scoped_path_already_present: bool = False

    @property
    def new_site(self) -> str:
        return module_to_path(self.spec.new_module)

    def describe(self) -> str:
        lines = [
            f"{len(self.id_rewrites)} ledger id(s) move {self.spec.old_prefix}* -> {self.spec.new_prefix}*",
        ]
        lines += [f"  {old}  ->  {new}" for old, new in sorted(self.id_rewrites.items())]
        if self.scoped_path_already_present:
            lines.append(f"SCOPED_PATHS already contains {self.new_site}")
        elif self.scoped_path_added:
            lines.append(f"SCOPED_PATHS gains {self.scoped_path_added}")
        return "\n".join(lines)


def _ledger_ids(ledger_text: str) -> list[str]:
    return [m.group("id") for m in (_ID_LINE.match(line) for line in ledger_text.splitlines()) if m]


def plan_move(
    spec: MoveSpec,
    ledger_text: str,
    scoped_text: str,
) -> MovePlan:
    """Work out every id rewrite and the `SCOPED_PATHS` addition. Writes nothing."""
    plan = MovePlan(spec=spec)
    for identifier in _ledger_ids(ledger_text):
        new = spec.rewrite_id(identifier)
        if new is not None:
            plan.id_rewrites[identifier] = new
    new_site = module_to_path(spec.new_module)
    if f'"{new_site}"' in scoped_text or f"'{new_site}'" in scoped_text:
        plan.scoped_path_already_present = True
    else:
        plan.scoped_path_added = new_site
    return plan


def apply_move(
    plan: MovePlan,
    ledger_text: str,
    scoped_text: str,
) -> tuple[str, str]:
    """Return the rewritten (ledger_text, scoped_text) for `plan`. Pure; writes nothing."""
    old_site = module_to_path(plan.spec.old_module)
    out: list[str] = []
    # `in_moved_entry` tracks whether the entry currently being read is one whose
    # id this plan rewrites, so only THAT entry's `site:` is re-pointed -- the
    # other entries still in the old file must keep the old path.
    in_moved_entry = False
    for line in ledger_text.splitlines(keepends=True):
        id_match = _ID_LINE.match(line.rstrip("\n"))
        if id_match:
            identifier = id_match.group("id")
            new_id = plan.id_rewrites.get(identifier)
            in_moved_entry = new_id is not None
            if new_id is not None:
                line = f"{id_match.group('indent')}- id: {new_id}\n"
            out.append(line)
            continue
        if in_moved_entry:
            site_match = _SITE_LINE.match(line.rstrip("\n"))
            if site_match and site_match.group("site") == old_site:
                line = f"{site_match.group('indent')}site: {plan.new_site}\n"
        out.append(line)
    new_ledger = "".join(out)

    new_scoped = scoped_text
    if plan.scoped_path_added:
        anchor = f'    "{old_site}",\n'
        if anchor not in scoped_text:
            raise ValueError(
                f"cannot anchor the SCOPED_PATHS insertion: {old_site!r} is not listed "
                "in the form this helper edits; add the new path by hand and re-verify"
            )
        new_scoped = scoped_text.replace(anchor, anchor + f'    "{plan.scoped_path_added}",\n', 1)
    return new_ledger, new_scoped


def verify_move(
    plan: MovePlan,
    ledger_text: str,
    scoped_text: str,
) -> list[str]:
    """Re-read an ALREADY-APPLIED result and return every problem found.

    An empty list means the move landed exactly as planned. This is the half that
    makes the helper checkable rather than trusted: it is run against the files on
    disk after the edit, not against the helper's own return value.
    """
    problems: list[str] = []
    ids = _ledger_ids(ledger_text)
    present = set(ids)

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    for dupe in duplicates:
        problems.append(f"duplicate ledger id after the move: {dupe}")

    for old, new in sorted(plan.id_rewrites.items()):
        if old in present:
            problems.append(f"old ledger id still present: {old}")
        if new not in present:
            problems.append(f"new ledger id missing: {new}")

    leftovers = sorted(i for i in present if plan.spec.rewrite_id(i) is not None)
    for leftover in leftovers:
        if leftover not in plan.id_rewrites:
            problems.append(f"id matches the move but was not rewritten: {leftover}")

    new_site = plan.new_site
    if f'"{new_site}"' not in scoped_text and f"'{new_site}'" not in scoped_text:
        problems.append(f"{new_site} is not in SCOPED_PATHS -- its numbers would drop out of the ledger guard silently")
    return problems


def ledger_ids_for_module(module: str, ledger_text: str) -> list[str]:
    """Every ledger id whose module component is exactly `module`."""
    prefix = module + "."
    return sorted(
        i
        for i in _ledger_ids(ledger_text)
        # `src.pipeline.` must not swallow `src.pipeline_stages.`; the next
        # segment after the prefix is what distinguishes them, and the prefix
        # itself already ends in the dot that separates them.
        if i.startswith(prefix)
    )
