"""The live-capital pre-flight gate (board item 150).

Until now the conditions that must hold before real money is switched on lived
only in prose — `docs/FUTURE.md` ("Before any live work is authorized"),
`docs/STATE.md` ("Not authorized"), `docs/OUTCOME.md` (MVP lifecycle) and the
`[[qamc-live-capital-checklist]]` anchor referenced from
`docs/QAMC_REMEDIATION_SPEC.md` and `src/config.py`. Nothing enforced them, so a
live-capital switch taken with an unmet condition would neither be stopped nor
even noticed.

This module turns that prose into a mechanical gate. It enumerates each named
pre-flight condition, reports each condition's current state, and BLOCKS
(non-zero exit / ``passed is False``) unless *every* condition is satisfied.

Two kinds of condition:

* **mechanical** — checkable in code here and now (is the paper-only guard still
  active, does the shipped config still declare paper). Any error checking a
  mechanical condition is treated as a FAIL: the gate fails closed.
* **manual-attestation** — inherently a human judgment (has the owner approved,
  has the strategy earned live capital, is the threat model designed, has the
  2.0x paper margin been re-derived for live …). These cannot be proven in code,
  so rather than silently pass them the gate requires a signed attestation in
  ``config/live_capital_preflight_attestations.yaml``. An absent, false or
  incomplete attestation BLOCKS.

This module invents no trading thresholds. The condition list is sourced from
the board item's DONE WHEN and the checklist prose named above; the mechanical
checks only read state that already exists.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ATTESTATIONS_PATH = (
    PROJECT_ROOT / "config" / "live_capital_preflight_attestations.yaml"
)

_PAPER_HOST = "paper-api.alpaca.markets"

# Result statuses.
PASS = "PASS"
FAIL = "FAIL"
BLOCK = "BLOCK"  # manual condition not (yet) attested


@dataclass
class ConditionResult:
    condition_id: str
    title: str
    kind: str  # "mechanical" | "manual"
    source: str
    status: str  # PASS | FAIL | BLOCK
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == PASS


@dataclass
class GateResult:
    results: list[ConditionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when EVERY condition passed. Empty result set never passes."""
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def blocking(self) -> list[ConditionResult]:
        return [r for r in self.results if not r.ok]


# --------------------------------------------------------------------------- #
# Mechanical checks
# --------------------------------------------------------------------------- #
def _check_paper_only_guard() -> tuple[str, str]:
    """The one-token-config-edit guard must reject a non-paper account.

    Source: src/config.py AlpacaConfig._enforce_paper_only — "going live must
    never be a casual config toggle" (docs/FUTURE.md). We prove the guard is
    still wired by constructing a non-paper config and requiring it to raise.
    """
    try:
        from src.config import AlpacaConfig
    except Exception as exc:  # pragma: no cover - import failure => fail closed
        return FAIL, f"could not import AlpacaConfig ({exc!r})"

    try:
        AlpacaConfig(base_url=f"https://{_PAPER_HOST}", paper=False)
    except Exception:
        return PASS, "AlpacaConfig rejects paper=False (config-toggle guard active)"
    return FAIL, "AlpacaConfig accepted paper=False — the paper-only guard is GONE"


def _check_settings_declare_paper(settings_path: Path) -> tuple[str, str]:
    """The shipped config must still declare a paper account and paper host.

    Source: docs/STATE.md / docs/OUTCOME.md — Alpaca Paper is the only
    authorized environment. A pre-flight run should confirm the live switch has
    not already been flipped silently.
    """
    try:
        raw = yaml.safe_load(settings_path.read_text()) or {}
    except FileNotFoundError:
        return FAIL, f"settings file not found: {settings_path}"
    except Exception as exc:  # pragma: no cover - malformed yaml => fail closed
        return FAIL, f"could not read settings ({exc!r})"

    alpaca = raw.get("alpaca")
    if not isinstance(alpaca, dict):
        return FAIL, "settings.yaml has no 'alpaca' section"
    if alpaca.get("paper") is not True:
        return FAIL, f"alpaca.paper is {alpaca.get('paper')!r}, expected true"
    host = str(alpaca.get("base_url", "")).strip().lower()
    if _PAPER_HOST not in host:
        return FAIL, f"alpaca.base_url does not point at the paper host: {host!r}"
    return PASS, "settings.yaml declares alpaca.paper: true on the paper host"


# --------------------------------------------------------------------------- #
# Condition registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MechanicalCondition:
    condition_id: str
    title: str
    source: str
    checker: Callable[..., tuple[str, str]]
    needs_settings: bool = False


@dataclass(frozen=True)
class ManualCondition:
    condition_id: str
    title: str
    source: str


# Mechanical conditions — checkable in code.
MECHANICAL_CONDITIONS: tuple[MechanicalCondition, ...] = (
    MechanicalCondition(
        condition_id="paper_only_guard_active",
        title="The paper-only guard still rejects a non-paper config",
        source="src/config.py AlpacaConfig._enforce_paper_only; docs/FUTURE.md",
        checker=_check_paper_only_guard,
    ),
    MechanicalCondition(
        condition_id="settings_declare_paper",
        title="Shipped config still declares a paper account on the paper host",
        source="config/settings.yaml; docs/STATE.md; docs/OUTCOME.md",
        checker=_check_settings_declare_paper,
        needs_settings=True,
    ),
)

# Manual-attestation conditions — inherently human judgment. Sourced verbatim
# from docs/FUTURE.md "Before any live work is authorized" plus the named
# conditions in docs/STATE.md and src/config.py §11.2 / QAMC_REMEDIATION_SPEC.
MANUAL_CONDITIONS: tuple[ManualCondition, ...] = (
    ManualCondition(
        "explicit_owner_approval",
        "Explicit owner approval for live-capital activation is recorded",
        "docs/FUTURE.md; docs/STATE.md (Not authorized)",
    ),
    ManualCondition(
        "strategy_earned_live_capital",
        "Evidence the strategy has earned live capital",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "broker_capabilities_reviewed",
        "Broker capabilities at that time have been reviewed",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "threat_model_and_credential_design",
        "Threat model and credential design are complete",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "governor_and_sentinel_specs",
        "Execution-governor and Sentinel specs with failure modes exist",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "breaker_thresholds_and_escalation_matrix",
        "Circuit-breaker thresholds and escalation matrix are defined",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "broker_local_reconciliation",
        "Broker/local reconciliation has been verified",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "failure_mode_tests_passed",
        "Stop-lifecycle, network, VPS, provider, split-brain and stale-state "
        "failure tests have passed",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "deployment_change_control_incident_recovery",
        "Deployment, change-control, incident and recovery procedures exist",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "capital_promotion_criteria",
        "Capital-promotion criteria (graduated capital) are defined",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "live_readiness_review_signed",
        "The full live-readiness review has been completed and signed",
        "docs/FUTURE.md",
    ),
    ManualCondition(
        "margin_2x_rederived_for_live",
        "The 2.0x paper-account margin cap has been re-derived for live capital",
        "src/config.py §11.2; docs/QAMC_REMEDIATION_SPEC.md",
    ),
)


# --------------------------------------------------------------------------- #
# Attestations
# --------------------------------------------------------------------------- #
def load_attestations(path: Path) -> dict:
    """Load the attestation file. Any problem returns an empty mapping so the
    gate fails closed (every manual condition then BLOCKS)."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    att = raw.get("attestations")
    return att if isinstance(att, dict) else {}


def _evaluate_manual(cond: ManualCondition, attestations: dict) -> ConditionResult:
    entry = attestations.get(cond.condition_id)
    if not isinstance(entry, dict):
        detail = "no attestation on file"
    elif entry.get("attested") is not True:
        detail = "not attested (attested is not true)"
    elif not entry.get("attested_by"):
        detail = "attested but 'attested_by' is missing"
    elif not entry.get("attested_on"):
        detail = "attested but 'attested_on' is missing"
    else:
        note = entry.get("note")
        suffix = f" — {note}" if note else ""
        return ConditionResult(
            cond.condition_id, cond.title, "manual", cond.source, PASS,
            f"attested by {entry['attested_by']} on {entry['attested_on']}{suffix}",
        )
    return ConditionResult(
        cond.condition_id, cond.title, "manual", cond.source, BLOCK, detail
    )


def evaluate(
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    attestations_path: Path = DEFAULT_ATTESTATIONS_PATH,
) -> GateResult:
    """Evaluate every pre-flight condition. Defaults to BLOCK on any failure."""
    results: list[ConditionResult] = []

    for mech in MECHANICAL_CONDITIONS:
        try:
            if mech.needs_settings:
                status, detail = mech.checker(settings_path)
            else:
                status, detail = mech.checker()
        except Exception as exc:  # fail closed on any checker error
            status, detail = FAIL, f"checker raised {exc!r}"
        results.append(
            ConditionResult(
                mech.condition_id, mech.title, "mechanical", mech.source,
                status, detail,
            )
        )

    attestations = load_attestations(attestations_path)
    for manual in MANUAL_CONDITIONS:
        results.append(_evaluate_manual(manual, attestations))

    return GateResult(results=results)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_report(gate: GateResult) -> str:
    lines: list[str] = []
    lines.append("Live-capital pre-flight gate (board item 150)")
    lines.append("=" * 60)
    for r in gate.results:
        mark = {PASS: "PASS ", FAIL: "FAIL ", BLOCK: "BLOCK"}[r.status]
        lines.append(f"[{mark}] {r.condition_id} ({r.kind})")
        lines.append(f"        {r.title}")
        lines.append(f"        state : {r.detail}")
        lines.append(f"        source: {r.source}")
    lines.append("=" * 60)
    if gate.passed:
        lines.append("RESULT: all conditions satisfied — live capital may proceed.")
    else:
        n = len(gate.blocking)
        lines.append(
            f"RESULT: BLOCKED — {n} condition(s) unmet. Live capital must NOT be "
            "activated."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Live-capital pre-flight gate (board item 150). "
        "Exits non-zero (BLOCK) unless every condition is satisfied.",
    )
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument(
        "--attestations", type=Path, default=DEFAULT_ATTESTATIONS_PATH
    )
    args = parser.parse_args(argv)

    gate = evaluate(settings_path=args.settings, attestations_path=args.attestations)
    print(format_report(gate))
    return 0 if gate.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
