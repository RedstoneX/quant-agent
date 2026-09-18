"""Fail the build when a new trade-governing number arrives with no source.

THE DEFECT. The desk's hardest standing rule (`docs/OUTCOME.md`, and the top
of `docs/WORK.md`) is that a number which governs a trade must be read off the
instrument, derived from something that is itself read off the instrument, or
carry a written derivation at its definition site. Approval does not make a
flat number non-arbitrary, and neither does backtesting it into place. On
2026-09-11 an audit listed about twenty such numbers; on 2026-09-14 every one
was re-checked and every one was still live; the finding was filed as "an
inventory, not an item / never re-audit" and nothing was assigned. A week
later they were all still live and the count had grown.

That is the real defect. Not the list — the absence of any boundary. **There
was no mechanical check of any kind**, so the next invented number got in for
free, and did: `ConstructorConfig.min_risk_pct` refuses every trade plan sized
under 0.50% of the account, exists in exactly one place as a bare dataclass
default, appears in no config file, no document and no ratification record,
and the comment eight lines above it calls the same figure "a constructor
default nobody chose".

WHAT THIS MODULE DOES. It enumerates every numeric DEFINITION SITE inside a
declared scope, and requires each one to carry an entry in a checked-in
ledger (`config/number_ledger.yaml`) saying where the number came from. A new
number in scope with no ledger entry fails `pytest`, which is the check
branch protection requires. The ledger is the inventory, and because the
build reads it, it is an inventory that cannot rot unnoticed.

WHY A REGISTRY AND NOT AN ANNOTATION. A required comment marker (`# source:`)
was the cheaper design and was rejected: the failure this closes is that
nobody remembers the rule, and a marker is only present if somebody
remembered to type it — the check would have to accept its absence as "not a
trade number" and would therefore catch nothing. The ledger inverts that. The
scanner decides what is in scope from the code's own structure; the author
cannot opt out by staying quiet, only by writing an entry somebody reviews.

WHY SCOPE IS A DECLARED FILE LIST AND TWO STRUCTURAL RULES, NOT A HEURISTIC.
The hard part is that a trade-governing number does not look different from
any other number. A scanner that guesses drowns: there are 1,256 numeric
literals in these files alone, and a whole-tree scan is worse. Guessing from
the NAME ("pct", "atr", "risk") fails on `min_risk_pct` in both directions —
it would also flag every unrelated percentage in the codebase. So nothing is
guessed. Scope is:

  * an explicit list of the modules that decide, size, price or exit a trade
    (`SCOPED_PATHS` below), reviewed as a list; and, inside those files only,
  * (a) module-level UPPER_CASE names bound to a numeric literal, and
    (b) numeric defaults on fields of a class whose name ends in `Config`.

Rule (b) is what makes the `min_risk_pct` case catchable without flagging
every dataclass default in the repo. `*Config` is already this codebase's
settled name for "the tunables", and every result/DTO dataclass in the same
files (`GrossCeilingOutcome`, `PortfolioVolEstimate`, `SizingDecision`) is
excluded by it without a special case. A new tunable added to an existing
`*Config`, or a new `*Config` class in a scoped file, is in scope the moment
it is written — which is the only property that matters, because the author
of the next number will not have read this docstring.

Numeric leaves inside tuple, list and dict literals are sites too. That is
not completeness for its own sake: `stop_atr_setup_scale` holds the stop-width
scalers as a tuple of pairs, and those multiply into every stop distance.

FOUR THINGS THE LEDGER IS CHECKED FOR, not one:

  1. COVERAGE — every in-scope site has an entry. A new number fails.
  2. VALUE — the entry's recorded value equals the live literal. Changing a
     number without opening its ledger entry fails, so the derivation is
     re-read by whoever changes it rather than outliving it silently.
  3. GROUNDS — `sourced` requires prose saying where it was read from;
     `derived` requires naming another ledger entry it came from.
  4. BASE DRIFT — a `derived` entry records `base_value`, the value its base
     held when the derivation was written. If the base later moves, the two
     disagree and the build fails. **This is the class the brief asked
     about**: the `range` stop scaler 0.90 was justified against a base that
     changed on 2026-09-10 (`git log -1 0088328c`, base 3.0 -> 2.5), at which
     point its justification stopped describing the live geometry. Rule 4
     catches that class going forward, for any number whose derivation is
     recorded as a derivation. It cannot catch it retroactively, and it
     cannot catch a number justified against something outside the ledger —
     see the limits at the bottom of this docstring.

WHAT THIS STRUCTURALLY CANNOT CATCH, stated here so the module is never cited
as if it covered more:

  * A number OUTSIDE `SCOPED_PATHS`. Scope is a reviewed list, and a trade
    number written into an unscoped module is invisible. Widening scope is a
    one-line edit; remembering to is exactly the thing that slips.
  * A number computed at run time from other numbers, or read from
    `config/settings.yaml` for a field the scanner sees only as its default.
    The ledger covers the default in code; a settings override is a different
    boundary (`src/agents/prompt_limits.py` owns the prompt half of it).
  * A number in prompt PROSE. Measured 2026-09-18: ~1,825 numeric tokens
    across `config/prompts/*.md`, overwhelmingly dates and list numbering.
    That is board item 105 and it is not solvable this way.
  * A WRONG source. Nothing here reads the citation and checks it is true. An
    author who writes "read from the instrument" about a number they invented
    passes. This gate makes the claim explicit and reviewable; it does not
    adjudicate it.
  * A number that was arbitrary all along. Everything live on the day this
    shipped is seeded into the ledger with its honest classification: of 122
    sites in scope, 87 are marked `arbitrary`. The gate holds the boundary; it
    does not
    retroactively source anything. `MAX_ARBITRARY_ENTRIES` ratchets that count
    so the list can shrink and cannot quietly grow.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Repository root, resolved from this file rather than the cwd so the check
#: behaves the same under pytest, a git hook and a direct run.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The ledger. Data, not code: it holds no value the desk trades on, only a
#: record of where each traded value is supposed to have come from.
LEDGER_PATH = REPO_ROOT / "config" / "number_ledger.yaml"

#: The modules that decide, size, price or exit a trade. THIS LIST IS THE
#: SCOPE and is meant to be read as a list, not extended by reflex. A
#: directory entry covers every `.py` under it.
SCOPED_PATHS: tuple[str, ...] = (
    "src/risk",
    "src/portfolio_constructor.py",
    "src/rotation.py",
    "src/nominations.py",
    "src/evidence_gate.py",
    "src/data/correlation.py",
    "src/execution/cash_sweep.py",
    "src/execution/stop_records.py",
)

#: Config classes inside scoped files whose numeric field defaults are sites.
#: Matched by SUFFIX, so a new `FooConfig` is covered the day it is written.
CONFIG_CLASS_SUFFIX = "Config"

#: `src/config.py` holds every seat's settings in one file, most of them
#: nothing to do with a trade (LLM cost circuits, Telegram retries, evolution
#: bookkeeping). Scoping the whole file would bury the signal, so the
#: trade-governing classes are named. Same suffix rule applies inside them.
SCOPED_CONFIG_CLASSES: tuple[str, ...] = (
    "RiskConfig",
    "ExecutionConfig",
    "CashSweepConfig",
    "IntradayScanConfig",
    "SmartMoneyConfig",
    "NominationConfig",
    "EventRiskConfig",
)
SCOPED_CONFIG_MODULE = "src/config.py"

#: Zero and one are excluded as definition sites. Not a convenience: neither
#: is a chosen magnitude. 0 is an empty/neutral default on a result field, and
#: 1 as a multiplier or a count of one is the identity, not a setting. Every
#: number the audits found is outside this set. Excluding them removes ~40
#: sites that no reviewer would have anything to say about.
NEUTRAL_VALUES: frozenset[float] = frozenset({0.0, 1.0, -1.0})

#: Classifications a ledger entry may carry.
#:   instrument       — read off the instrument at run time or fixed by an
#:                      external spec the desk does not choose (a broker tick
#:                      size, an exchange rule). Requires `source`.
#:   sourced          — a cited derivation or published source written down.
#:                      Requires `source`.
#:   derived          — computed from another ledger entry. Requires
#:                      `derived_from` and `base_value`.
#:   arbitrary        — live, governs trades, and nothing backs it. Permitted,
#:                      because these already exist and changing one is the
#:                      owner's call — but counted and ratcheted.
#:   not-trade-governing — in scope structurally, does not reach a trade
#:                      decision. Requires `note` saying why.
VALID_STATUSES: frozenset[str] = frozenset(
    {"instrument", "sourced", "derived", "arbitrary", "not-trade-governing"}
)

#: Ratchet. The seeded ledger records this many `arbitrary` entries; the build
#: fails if the count rises. Adding one more unsourced trade number is then a
#: visible edit to a declared ceiling in a reviewed file, not a quiet default.
#: LOWER THIS when one is sourced. Never raise it without an owner ruling.
MAX_ARBITRARY_ENTRIES = 87


@dataclass(frozen=True)
class NumberSite:
    """One numeric definition site the ledger must account for."""

    site_id: str
    path: str
    lineno: int
    value: float

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.site_id} = {self.value!r}  ({self.path}:{self.lineno})"


@dataclass
class LedgerProblem:
    """One reason the build should fail, in the words a reviewer needs."""

    kind: str
    site_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"[{self.kind}] {self.site_id}: {self.detail}"


def _numeric(node: ast.AST) -> float | None:
    """The literal value of `node`, or None if it is not a numeric literal.

    Handles the unary minus that `ast` represents as an operator rather than
    as part of the constant, so `-20.0` is one site and not a miss.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand)
        return None if inner is None else -inner
    return None


def _leaves(node: ast.AST, prefix: str) -> list[tuple[str, float, int]]:
    """Every numeric leaf under `node`, with a stable path-qualified id.

    A bare literal yields one leaf. A tuple, list or dict literal yields one
    leaf per numeric element, keyed by index or by its literal key, so
    `stop_atr_setup_scale`'s `("range", 0.90)` is addressable as
    `...stop_atr_setup_scale[1][1]` and moves only if the structure moves.
    """
    value = _numeric(node)
    if value is not None:
        return [(prefix, value, getattr(node, "lineno", 0))]

    out: list[tuple[str, float, int]] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for index, element in enumerate(node.elts):
            out.extend(_leaves(element, f"{prefix}[{index}]"))
    elif isinstance(node, ast.Dict):
        for key, element in zip(node.keys, node.values):
            if isinstance(key, ast.Constant):
                label = f"[{key.value!r}]"
            else:
                label = "[?]"
            out.extend(_leaves(element, f"{prefix}{label}"))
    return out


def _field_default(node: ast.AnnAssign) -> ast.AST | None:
    """The default expression of an annotated class field, or None.

    Covers a bare default (`x: float = 2.5`), a dataclass `field(default=...)`
    and a pydantic `Field(2.5, ...)` / `Field(default=2.5)`. A
    `default_factory` is deliberately NOT followed: the number then lives in
    a function body, which is out of scope and honestly declared as such.
    """
    if node.value is None:
        return None
    if isinstance(node.value, ast.Call):
        for keyword in node.value.keywords:
            if keyword.arg == "default":
                return keyword.value
        if node.value.args:
            return node.value.args[0]
        return None
    return node.value


def _scan_module(path: Path, rel: str, config_classes: tuple[str, ...] | None) -> list[NumberSite]:
    """Sites in one file.

    `config_classes` None means "any class whose name ends in `Config`" — the
    rule for a scoped module. A tuple means only those names, which is how
    `src/config.py` is handled without pulling in the LLM and Telegram
    settings that share the file.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = rel[: -len(".py")].replace("/", ".")
    sites: list[NumberSite] = []

    # (a) module-level UPPER_CASE constants.
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not (name.isupper() or (name.startswith("_") and name.lstrip("_").isupper())):
                continue
            for site_id, value, lineno in _leaves(node.value, f"{module}.{name}"):
                if value not in NEUTRAL_VALUES:
                    sites.append(NumberSite(site_id, rel, lineno, value))

    # (b) numeric defaults on `*Config` class fields.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if config_classes is None:
            if not node.name.endswith(CONFIG_CLASS_SUFFIX):
                continue
        elif node.name not in config_classes:
            continue
        for body_node in node.body:
            if not isinstance(body_node, ast.AnnAssign):
                continue
            if not isinstance(body_node.target, ast.Name):
                continue
            default = _field_default(body_node)
            if default is None:
                continue
            base = f"{module}.{node.name}.{body_node.target.id}"
            for site_id, value, lineno in _leaves(default, base):
                if value not in NEUTRAL_VALUES:
                    sites.append(NumberSite(site_id, rel, lineno, value))

    return sites


def collect_sites(repo_root: Path | None = None) -> list[NumberSite]:
    """Every in-scope numeric definition site in the tree, sorted by id."""
    root = repo_root or REPO_ROOT
    sites: list[NumberSite] = []

    for entry in SCOPED_PATHS:
        target = root / entry
        if target.is_dir():
            files = sorted(target.rglob("*.py"))
        elif target.is_file():
            files = [target]
        else:
            # A scoped path that no longer exists is a finding in itself:
            # scope silently narrowing is how this check would rot.
            raise FileNotFoundError(f"SCOPED_PATHS entry does not exist: {entry}")
        for file_path in files:
            if file_path.name == "__init__.py" and file_path.stat().st_size == 0:
                continue
            sites.extend(
                _scan_module(file_path, str(file_path.relative_to(root)), None)
            )

    config_module = root / SCOPED_CONFIG_MODULE
    if not config_module.is_file():
        raise FileNotFoundError(f"missing {SCOPED_CONFIG_MODULE}")
    sites.extend(_scan_module(config_module, SCOPED_CONFIG_MODULE, SCOPED_CONFIG_CLASSES))

    return sorted(sites, key=lambda s: s.site_id)


def load_ledger(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ledger as `{site_id: entry}`. Raises on a duplicate id."""
    ledger_path = path or LEDGER_PATH
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("numbers") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        site_id = entry.get("id")
        if not site_id:
            raise ValueError(f"ledger entry with no id: {entry!r}")
        if site_id in out:
            raise ValueError(f"duplicate ledger id: {site_id}")
        out[site_id] = entry
    return out


def audit(
    repo_root: Path | None = None, ledger_path: Path | None = None
) -> list[LedgerProblem]:
    """Every reason the build should fail. Empty means the boundary holds."""
    sites = collect_sites(repo_root)
    ledger = load_ledger(ledger_path)
    by_id = {site.site_id: site for site in sites}
    problems: list[LedgerProblem] = []

    # 1. COVERAGE. A number in scope with no entry is the whole point.
    for site in sites:
        if site.site_id not in ledger:
            problems.append(
                LedgerProblem(
                    "unsourced",
                    site.site_id,
                    f"new trade-governing number {site.value!r} at "
                    f"{site.path}:{site.lineno} with no entry in "
                    f"config/number_ledger.yaml. Say where it came from, or "
                    f"record it as arbitrary and lower nothing.",
                )
            )

    # A ledger entry with no site is stale bookkeeping, and left alone it
    # would let coverage look complete while the file drifted.
    for site_id in ledger:
        if site_id not in by_id:
            problems.append(
                LedgerProblem(
                    "orphan",
                    site_id,
                    "ledger entry no longer matches any definition site — the "
                    "number was renamed, moved or deleted. Remove the entry or "
                    "point it at the new site.",
                )
            )

    for site_id, entry in ledger.items():
        site = by_id.get(site_id)
        status = entry.get("status")

        if status not in VALID_STATUSES:
            problems.append(
                LedgerProblem(
                    "bad-status",
                    site_id,
                    f"status {status!r} is not one of {sorted(VALID_STATUSES)}",
                )
            )
            continue

        # 2. VALUE. The recorded value must still be the live one, so that
        #    changing a number puts its justification back in front of the
        #    person changing it.
        if site is not None:
            recorded = entry.get("value")
            if recorded is None or abs(float(recorded) - site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "value-drift",
                        site_id,
                        f"live value {site.value!r} but the ledger records "
                        f"{recorded!r}. If the number changed on purpose, "
                        f"re-read its justification and update the entry.",
                    )
                )

        # 3. GROUNDS.
        if status in ("sourced", "instrument") and not str(entry.get("source") or "").strip():
            problems.append(
                LedgerProblem(
                    "no-source",
                    site_id,
                    f"status {status!r} requires a `source:` saying where the "
                    f"number was read from.",
                )
            )
        if status == "not-trade-governing" and not str(entry.get("note") or "").strip():
            problems.append(
                LedgerProblem(
                    "no-note",
                    site_id,
                    "status 'not-trade-governing' requires a `note:` saying why "
                    "this number cannot reach a trade decision.",
                )
            )

        # 4. BASE DRIFT. The class from the brief: sourced once, unsourced
        #    later because what it was derived from moved underneath it.
        if status == "derived":
            base_id = entry.get("derived_from")
            if not base_id:
                problems.append(
                    LedgerProblem(
                        "no-base",
                        site_id,
                        "status 'derived' requires `derived_from:` naming the "
                        "ledger entry this was computed from.",
                    )
                )
                continue
            if "base_value" not in entry:
                problems.append(
                    LedgerProblem(
                        "no-base-value",
                        site_id,
                        "status 'derived' requires `base_value:` — the value "
                        "the base held when this derivation was written. "
                        "Without it a moving base is undetectable.",
                    )
                )
                continue
            base_site = by_id.get(base_id)
            if base_site is None:
                problems.append(
                    LedgerProblem(
                        "unknown-base",
                        site_id,
                        f"derived_from {base_id!r} is not a definition site in "
                        f"scope. A derivation can only be tracked against a "
                        f"number this check can see.",
                    )
                )
                continue
            if abs(float(entry["base_value"]) - base_site.value) > 1e-12:
                problems.append(
                    LedgerProblem(
                        "base-drift",
                        site_id,
                        f"derived from {base_id} when it was "
                        f"{entry['base_value']!r}; it is now {base_site.value!r}. "
                        f"The derivation no longer describes the live geometry, "
                        f"so this number is arbitrary again. Re-derive it, or "
                        f"reclassify it honestly.",
                    )
                )

    # 5. RATCHET. The unsourced list may shrink. It may not grow by default.
    arbitrary = [i for i, e in ledger.items() if e.get("status") == "arbitrary"]
    if len(arbitrary) > MAX_ARBITRARY_ENTRIES:
        problems.append(
            LedgerProblem(
                "ratchet",
                "<ledger>",
                f"{len(arbitrary)} entries are classified 'arbitrary' but "
                f"MAX_ARBITRARY_ENTRIES is {MAX_ARBITRARY_ENTRIES}. Adding an "
                f"unsourced trade-governing number is an owner decision "
                f"(docs/OUTCOME.md); raising this ceiling is how you record "
                f"that one was made.",
            )
        )

    return sorted(problems, key=lambda p: (p.kind, p.site_id))


def main() -> int:  # pragma: no cover - CLI convenience
    problems = audit()
    if not problems:
        sites = collect_sites()
        print(f"number ledger: {len(sites)} sites in scope, all accounted for")
        return 0
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} problem(s). See src/number_sources.py for the rules.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
