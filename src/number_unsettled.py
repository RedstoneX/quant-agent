"""UNSETTLED, NO ROUTE YET -- the fourth recorded state of a ledger row.

WHY THIS EXISTS. `src/number_sources.py` requires every `status: arbitrary`
row to sit in item 90's state 3: unsourceable today, but with a named
recording -- built or specified -- that would settle it. The count of
`arbitrary` rows carrying NO such route is ratcheted to an equality, so it
cannot rise. That pair of rules has a hole, measured 2026-10-05: a number
that is genuinely arbitrary and that NOTHING in this desk is going to
measure cannot be entered in the ledger AT ALL. An agent carrying five such
sites could land none of them, because the only way past the ratchet is to
write down a settlement route nobody intends to build.

The register therefore systematically excluded exactly the numbers the
owner's standing order is about, and the rule pressured its writers to
fabricate a route. This module makes "unsettled, no route yet" a FIRST-CLASS
RECORDED STATE: writable, visible in the audit line, and counted.

WHY THIS IS NOT A LOOSENING. Before it, such a number lived in the source
with no ledger row at all and the headline count never saw it. After it, the
number is on the register, carries its `open_question` and
`cost_while_unanswered` like every other arbitrary row, carries a written
reason why no route exists, and is counted in the audit output. Strictly
more is recorded and strictly more is counted. Nothing that could be
registered before can be registered more cheaply now -- see the drift rule.

WHY IT IS NOT AN ESCAPE HATCH. Three rules, all derived at check time
against `origin/main`, none storing a list or a count anywhere:

  1. DRIFT. A row that already exists on trunk may only be `unsettled` here
     if it was ALREADY `unsettled` there, or if it was part of trunk's
     route-less residue (arbitrary, no route, no `unsettled`) -- which is a
     strict improvement, and which also lowers the stored route-less ratchet
     in `config/number_ledger_route_history.yaml`, so it cannot be done
     silently. Nothing with a settlement route, and nothing `sourced`,
     `derived` or `instrument`, can fall back into this state.

  2. NO RISE. Counting only rows that exist on trunk, the number of
     `arbitrary` rows with no valid settlement route -- route-less residue
     and `unsettled` together -- must not exceed what trunk carries. The
     population already on the register can only shrink. Brand-new sites are
     excluded from that count, because punishing registration is the defect
     this module was built to remove; their arrival is still gated by the
     `arbitrary` equality ratchet, which demands an appended history entry
     with a `why` for every new arbitrary row.

  3. SHAPE. `unsettled` is a mapping, is legal only on an `arbitrary` row,
     may never sit beside a `settles_by` block, and must carry prose in
     `why_no_route` at least as long as a settlement route's own prose
     minimum. A one-word reason is the same failure as a one-word route.

The trunk comparison stores NOTHING. An allow-list keyed on a count has been
found to be a hole three times in one week on this desk, and the week's work
was ripping stored bookkeeping out.
"""

from __future__ import annotations

import subprocess
from typing import Any, NamedTuple

import yaml

#: The one field an `unsettled` block must carry. Deliberately a single
#: field: every extra field is another place to write a route nobody intends
#: to build, which is the fabrication pressure this module exists to remove.
UNSETTLED_FIELDS: tuple[str, ...] = ("why_no_route",)

#: Where trunk's copy of the register is read from, and the ref it is read
#: at. Both are arguments everywhere below so the tests can point at a
#: fixture instead.
LEDGER_REPO_PATH = "config/number_ledger.yaml"
TRUNK_REF = "origin/main"


class UnsettledProblem(NamedTuple):
    """One reason a row's `unsettled` state is not honest. `kind` matches the
    shape `src/number_sources.py` prints, so these read like every other
    ledger problem in the same run."""

    kind: str
    site_id: str
    why: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.kind}] {self.site_id}: {self.why}"


def is_unsettled(entry: Any) -> bool:
    """True when this row declares the unsettled state in a usable shape.

    Shape errors are reported separately by `unsettled_problems`; this
    predicate only answers whether the row is CLAIMING the state, so a
    malformed claim cannot quietly fall back into the route-less residue and
    be counted twice.
    """
    return isinstance(entry, dict) and entry.get("unsettled") is not None


def _why_no_route(entry: Any) -> str:
    block = entry.get("unsettled") if isinstance(entry, dict) else None
    if not isinstance(block, dict):
        return ""
    return str(block.get("why_no_route", "")).strip()


def _has_route(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("settles_by") is not None


def _is_arbitrary(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("status") == "arbitrary"


def unanswered_sites(ledger: dict[str, Any]) -> set[str]:
    """Every `arbitrary` row with no settlement route: the route-less residue
    plus the rows that honestly declare themselves unsettled. This is the
    population rule 2 holds down, and it is computed, never stored."""
    return {
        site_id
        for site_id, entry in ledger.items()
        if _is_arbitrary(entry) and not _has_route(entry)
    }


def trunk_ledger(
    repo_root: Any, ref: str = TRUNK_REF, path: str = LEDGER_REPO_PATH
) -> dict[str, Any] | None:
    """Trunk's register, read through git. `None` when trunk cannot be read.

    A missing ref is the fixture tree and the shallow checkout, not a
    violation: the shape rules below still apply in full, and the drift and
    no-rise rules need a baseline to mean anything. Nothing is cached and
    nothing is written down.
    """
    try:
        raw = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    if raw.returncode != 0:
        return None
    try:
        loaded = yaml.safe_load(raw.stdout) or {}
    except yaml.YAMLError:  # pragma: no cover - trunk is parsed by its own build
        return None
    entries = loaded.get("entries") or loaded.get("numbers") or []
    if isinstance(entries, dict):
        return {str(k): v for k, v in entries.items()}
    out: dict[str, Any] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") is not None:
            out[str(entry["id"])] = entry
    return out


def shape_problems(ledger: dict[str, Any], min_prose_chars: int) -> list[UnsettledProblem]:
    """Rule 3. The `unsettled` block is legal only where it is honest."""
    problems: list[UnsettledProblem] = []
    for site_id, entry in sorted(ledger.items()):
        if not is_unsettled(entry):
            continue
        if not isinstance(entry.get("unsettled"), dict):
            problems.append(
                UnsettledProblem(
                    "unsettled",
                    site_id,
                    "`unsettled` is not a mapping. It must be a block carrying "
                    "`why_no_route`, because a bare marker records nothing a reader can weigh.",
                )
            )
            continue
        if not _is_arbitrary(entry):
            problems.append(
                UnsettledProblem(
                    "unsettled",
                    site_id,
                    f"declares `unsettled` but its status is {entry.get('status')!r}. A number that "
                    "states a source is answered; only an `arbitrary` row can be unsettled.",
                )
            )
        if _has_route(entry):
            problems.append(
                UnsettledProblem(
                    "unsettled",
                    site_id,
                    "carries BOTH `settles_by` and `unsettled`. A row either names the recording that "
                    "will settle it or says no such recording exists; claiming both hides which is true.",
                )
            )
        for field in UNSETTLED_FIELDS:
            if len(_why_no_route(entry)) < min_prose_chars:
                problems.append(
                    UnsettledProblem(
                        "unsettled",
                        site_id,
                        f"`unsettled.{field}` is under {min_prose_chars} characters. Say why nothing in "
                        "this desk will answer this number; a one-word reason is not a reason.",
                    )
                )
    return problems


def drift_problems(
    ledger: dict[str, Any], trunk: dict[str, Any]
) -> list[UnsettledProblem]:
    """Rules 1 and 2, both measured against trunk and both storing nothing."""
    problems: list[UnsettledProblem] = []
    trunk_unanswered = unanswered_sites(trunk)
    for site_id, entry in sorted(ledger.items()):
        if not is_unsettled(entry) or site_id not in trunk:
            continue
        if site_id in trunk_unanswered:
            continue
        problems.append(
            UnsettledProblem(
                "unsettled-drift",
                site_id,
                "is already on trunk in an answered or routed state and is being moved to `unsettled`. "
                "That is the escape hatch this state was built to refuse: a row may gain a route or a "
                "source, never lose one. Restore its route, or source the number.",
            )
        )
    now = len(unanswered_sites(ledger) & set(trunk))
    before = len(trunk_unanswered & set(ledger))
    if now > before:
        problems.append(
            UnsettledProblem(
                "unsettled-ratchet",
                "<ledger>",
                f"counting only rows trunk already carries, the number of `arbitrary` rows with no "
                f"settlement route rises from {before} to {now}. This population ratchets downward "
                f"only: an existing row leaves it by gaining a route or a source, never by another row "
                f"joining it. New sites are exempt and are gated by the `arbitrary` count ratchet.",
            )
        )
    return problems


def silence_problems(
    ledger: dict[str, Any], trunk: dict[str, Any]
) -> list[UnsettledProblem]:
    """Rule 3: a row trunk does not carry may never arrive SILENTLY route-less.

    This is what replaced the stored equality in
    `config/number_ledger_route_history.yaml`. That file held the route-less
    count as a hand-appended sum, which is the stored bookkeeping this desk
    spent the week ripping out and, worse, was the thing that made a genuinely
    unsettled number unwritable. The count is now derived from the two trees
    at check time and the rule is stricter than the sum it replaces: the set
    of silently route-less rows may only ever SHRINK, because a new row either
    names a route or declares `unsettled` with its reason in writing.
    """
    silent_on_trunk = {s for s in unanswered_sites(trunk) if not is_unsettled(trunk[s])}
    return [
        UnsettledProblem(
            "unsettled-silence",
            site_id,
            "is `arbitrary`, names no settlement route and says nothing about why, and trunk does "
            "not carry it that way. Silence only shrinks: name the recording that would settle it, "
            "or record an `unsettled:` block saying why no route exists. Both are cheaper than the "
            "invented route this rule used to pressure its readers into writing.",
        )
        for site_id, entry in sorted(ledger.items())
        if site_id not in trunk
        and _is_arbitrary(entry)
        and not _has_route(entry)
        and not is_unsettled(entry)
    ]


def unsettled_problems(
    ledger: dict[str, Any], repo_root: Any, min_prose_chars: int
) -> list[UnsettledProblem]:
    """Every problem with this register's unsettled rows, shape then drift."""
    problems = shape_problems(ledger, min_prose_chars)
    trunk = trunk_ledger(repo_root)
    if trunk is not None:
        problems.extend(drift_problems(ledger, trunk))
        problems.extend(silence_problems(ledger, trunk))
    return sorted(problems, key=lambda p: (p.kind, p.site_id))


def unsettled_count(ledger: dict[str, Any]) -> int:
    """How many rows sit in the unsettled state. Printed by the audit so the
    number is visible rather than merely permitted."""
    return sum(1 for entry in ledger.values() if is_unsettled(entry))
