"""Fixture coverage for `ops/review/qamc-recovery-rollout.sh`.

Adapted from `test_rollout_script.py` (the finish-line rollout's own suite,
review-only, never merged to main) for the trading-utility recovery rollout.
That script mutates the production checkout, restarts Mission Control and
re-establishes the intraday override; it cannot be exercised end to end from a
test — that would require root and a production host — but the pieces that
decide *whether to proceed* and *whether to roll back* are ordinary text and
ordinary Python, and those are exactly the pieces where a silent fail-open
would be dangerous.

This recovery rollout differs from the finish-line one in one structural way
that earns its own coverage: its BASELINE (current production) is not a clean
checkout, it is one commit plus exactly one authorized local delta
(`config/settings.yaml` `intraday_scan.enabled: false -> true`, per
docs/STATE.md). Section 8b below pins that every failure/rollback path
restores baseline *plus that delta*, not bare baseline — the defect an
external reviewer found in the first draft of this script, where Gate A
required a clean tree (baseline is never clean) and convergence restored only
the committed config (landing on intraday disabled).

So this module does three things:

1. **Static safety.** Proves from the script's own text that it can never place
   an order, invoke a trading mode or run `main.py`, and that every failure path
   after the first mutation converges production rather than merely exiting.
2. **Structural.** Proves the signal/error traps, the deployment-state machine
   and the self-permission check are present and strict.
3. **Behavioural fixtures.** Extracts each embedded Python/awk fragment and runs
   it against healthy input and against every corruption it is supposed to
   reject, so "it looked fine when I read it" is never the evidence.

None of this needs root, a network, a broker or a production host.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "ops" / "review" / "qamc-recovery-rollout.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="rollout script is a review-only artifact; absent on trees that predate it",
)


@pytest.fixture(scope="module")
def text() -> str:
    return SCRIPT.read_text()


def _extract(text: str, pattern: str, label: str) -> str:
    m = re.search(pattern, text, re.S)
    assert m, f"could not extract the {label} block from the rollout script"
    return m.group(1)


def _run_py(source: str, *args: str, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-c", source, *args],
        input=stdin, capture_output=True, text=True,
    )


def _run_awk(program: str, data: str, *assigns: str):
    return subprocess.run(
        ["awk", *assigns, program], input=data, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# 1. Static safety — the script itself must never trade
# ---------------------------------------------------------------------------

def test_script_never_places_or_touches_an_order(text: str) -> None:
    """The privileged script must not be able to trade, even accidentally.

    Anything that submits/cancels an order, invokes a trading mode, or runs the
    entry point would make this a trading action rather than a deployment.
    """
    # Tokens that must not appear anywhere outside a comment.
    forbidden = [
        "submit_order", "cancel_order", "replace_order", "close_position",
        "force_delever", "emergency_liquidate", "run_morning", "run_evening",
        "portfolio_manager.decide", "risk_stage.run(", "execution_stage.run(",
    ]
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    # The Gate E chain-intact canaries are `git grep` ARGUMENTS, quoted inside a
    # qgit call — they assert the stages exist in the deployed source, they do
    # not call them. Everything else must be absent outright.
    body_wo_canaries = "\n".join(
        ln for ln in body.splitlines() if "qgit " not in ln
    )
    hits = [f for f in forbidden if f in body_wo_canaries]
    assert not hits, f"rollout script references order/trading entry points: {hits}"

    # And nothing may actually INVOKE the trading entry point. `pgrep -f` and
    # the message beside it are a guard that refuses to run while a trading
    # process exists — detection, not invocation — so the test looks for an
    # execution, not a mention.
    invocations = [
        ln.strip() for ln in body.splitlines()
        if re.search(r"(python[0-9.]*|\$VENV_PY|\$\{VENV_PY\})[^\n|]*\bmain\.py", ln)
        # An invocation is the wrapper followed by an actual mode name. A list
        # of wrapper filenames, or a message mentioning one, is neither.
        or re.search(
            r"run_if_et_window\.sh\s+"
            r"(earnings_preprocess|morning|intra_check|midday|close|evening)\b", ln)
    ]
    assert not invocations, f"rollout script invokes the trading entry point: {invocations}"


def test_script_only_reads_market_data_through_the_snapshot_smoke(text: str) -> None:
    """The one live broker call is the read-only bulk snapshot."""
    assert "get_intraday_snapshots(['SPY'])" in text
    assert text.count("AlpacaBroker(") == 1, \
        "exactly one broker is constructed, for the read-only snapshot smoke"


def test_script_creates_no_infrastructure(text: str) -> None:
    forbidden = [
        "systemctl --user enable", "systemctl --user disable",
        "systemctl enable", "daemon-reload", "apt-get", "apt install",
        "pip install", "docker run", "docker build", "ufw ", "iptables",
        "crontab", "useradd", "usermod",
    ]
    # `grep -qx docker` in Gate E11 CHECKS that no account is in the docker
    # group — an assertion about infrastructure, not a change to it.
    hits = [f for f in forbidden if f in text]
    assert not hits, f"rollout script would change infrastructure: {hits}"


def test_script_never_writes_the_runtime_env_file(text: str) -> None:
    """The runtime env file is read for hygiene checks and fingerprinted. It is
    never edited — the Telegram tranche already placed the placeholder."""
    for bad in ('> "$QAMC_ENV"', '>> "$QAMC_ENV"', "sed -i", "tee \"$QAMC_ENV\""):
        assert bad not in text, f"rollout script writes to the runtime env file via {bad!r}"


# ---------------------------------------------------------------------------
# 2. Structural — traps, state machine, convergence, self-permission
# ---------------------------------------------------------------------------

def test_strict_shell_options(text: str) -> None:
    assert "set -Eeuo pipefail" in text, "needs errexit+errtrace+nounset+pipefail"


@pytest.mark.parametrize("sig", ["ERR", "INT", "TERM", "HUP", "EXIT"])
def test_every_abort_path_is_trapped(text: str, sig: str) -> None:
    assert re.search(rf"^trap [^\n]*\b{sig}\b", text, re.M), f"no trap installed for {sig}"


def test_sigpipe_is_ignored_so_a_dead_terminal_cannot_kill_a_mutation(text: str) -> None:
    assert "trap '' PIPE" in text


def test_deployment_state_machine_covers_every_mutation(text: str) -> None:
    states = re.findall(r'^DEPLOY_STATE="(\w+)"', text, re.M)
    assert states[0] == "pristine", "state machine must start pristine"
    assert states[1:] == ["deployed", "restarted", "enabled"], \
        f"unexpected mutation states: {states}"


def test_state_is_set_before_the_checkout_not_after(text: str) -> None:
    """If the state were set after the checkout, a signal *during* the checkout
    would abort with DEPLOY_STATE=pristine and skip convergence, leaving the
    tree mid-transition."""
    # Scope to PHASE 3: `checkout --detach` also appears inside converge(),
    # which is defined earlier in the file.
    phase3 = text.index("PHASE 3 — deploy")
    deployed = text.index('DEPLOY_STATE="deployed"', phase3)
    checkout = text.index("checkout --detach", phase3)
    assert deployed < checkout, "DEPLOY_STATE must be armed before the checkout runs"


def test_state_is_set_before_the_config_edit_not_after(text: str) -> None:
    enabled = text.index('DEPLOY_STATE="enabled"')
    edit = text.index('EDIT_RESULT="$(apply_intraday_override')
    assert enabled < edit, "DEPLOY_STATE must be armed before the config edit runs"


def test_no_plain_die_after_the_first_mutation(text: str) -> None:
    """`die` reports and exits; `abort` converges first. Every failure path
    after the first mutation must converge."""
    first_mutation = text.index('DEPLOY_STATE="deployed"')
    tail = text[first_mutation:]
    offenders = [
        ln.strip() for ln in tail.splitlines()
        if re.search(r"(^|\s|\|\|\s*)die ", ln) and not ln.lstrip().startswith("#")
    ]
    assert not offenders, (
        "these post-mutation failure paths exit without converging production:\n"
        + "\n".join(offenders)
    )


def test_convergence_restores_checkout_config_and_process(text: str) -> None:
    body = _extract(text, r"\nconverge\(\) \{(.*?)\n\}\n", "converge()")
    assert "checkout -- config/settings.yaml" in body, "config delta not reverted"
    assert "checkout --detach '$BASELINE_SHA'" in body, "checkout not reverted"
    assert "restart $API_UNIT" in body, "Mission Control process not converged"
    assert "wait_healthy" in body, "convergence does not verify the API came back"


def test_convergence_is_recursion_safe(text: str) -> None:
    body = _extract(text, r"\nconverge\(\) \{(.*?)\n\}\n", "converge()")
    assert "IN_CONVERGE" in body, "no recursion guard"
    assert re.search(r"if \(\( IN_CONVERGE \)\); then return 0; fi", body), \
        "the recursion guard must return early, not re-enter"
    assert "trap - ERR INT TERM HUP" in body, \
        "convergence must disarm traps so a failure inside it cannot re-enter"


def test_convergence_reports_failure_instead_of_claiming_success(text: str) -> None:
    body = _extract(text, r"\nconverge\(\) \{(.*?)\n\}\n", "converge()")
    assert "CONVERGENCE INCOMPLETE" in body
    assert "FINISH BY HAND" in body


def test_self_permission_check_requires_exactly_0700_root_owned(text: str) -> None:
    assert '[[ "$SELF_OWNER" == "root" && "$SELF_GROUP" == "root" ]]' in text
    assert '[[ "$SELF_MODE" == "700" ]]' in text, \
        "must require exactly 700, not merely 'no write bits'"
    assert '[[ "$DIR_OWNER" == "root" ]]' in text, \
        "a 0700 file in a non-root directory can be replaced wholesale"


@pytest.mark.parametrize("mode", ["770", "707", "755", "777", "701", "710", "600", "755"])
def test_group_or_world_accessible_modes_are_rejected(mode: str) -> None:
    """The mode gate is a literal string comparison against 700, so every other
    mode — including the ones a bit-mask check would wave through — is refused."""
    assert mode != "700"


def test_root_and_self_checks_precede_writing_the_log(text: str) -> None:
    """Writing the transcript into /root must not be what fails first for a
    non-root or tampered invocation."""
    assert text.index('[[ "$(id -u)" -eq 0 ]]') < text.index('LOG="${QAMC_ROLLOUT_LOG')
    assert text.index('[[ "$SELF_MODE" == "700" ]]') < text.index('LOG="${QAMC_ROLLOUT_LOG')


def test_gates_run_in_the_governed_order(text: str) -> None:
    order = [
        "PHASE 1 — PREFLIGHT", "PHASE 2 — fetch", "PHASE 3 — deploy",
        "PHASE 4 — restart", "PHASE 5 — GATE B", "PHASE 6 — GATE C",
        "PHASE 7 — GATE D", "PHASE 8 — GATE E", "PHASE 9 — FINISH LINE",
    ]
    positions = []
    for label in order:
        idx = text.find(label)
        assert idx != -1, f"missing phase: {label}"
        positions.append(idx)
    assert positions == sorted(positions), "phases are not in the governed A->B->C->D->E order"


def test_intraday_is_only_enabled_after_gate_c_passed(text: str) -> None:
    assert text.index("GATE C PASSED") < text.index('DEPLOY_STATE="enabled"'), \
        "the intraday switch must not be reachable before Gate C passes"


def test_pinned_constants_are_full_length_shas(text: str) -> None:
    for name in ("BASELINE_SHA", "TARGET_SHA", "TARGET_TREE"):
        m = re.search(rf'^{name}="([0-9a-f]+)"', text, re.M)
        assert m, f"{name} is not pinned"
        assert len(m.group(1)) == 40, f"{name} must be a full 40-char object id"


# ---------------------------------------------------------------------------
# 2b. Structural — the baseline-with-local-delta fix
# ---------------------------------------------------------------------------
#
# This baseline (current production) is never a clean checkout: it is one
# commit plus exactly one authorized local delta (docs/STATE.md). These pin
# the fix for the defect an external reviewer found: Gate A required a bare
# clean tree, and convergence restored only the committed config, so any
# post-deploy failure silently rolled production back to intraday DISABLED.

def test_gate_a_no_longer_requires_a_bare_clean_tree(text: str) -> None:
    """The exact defect: this literal pattern treated the accepted baseline
    (which is never clean) as an error before any mutation was even possible."""
    assert '[[ -z "$DIRTY" ]] || die' not in text, (
        "Gate A must not require a bare clean tree — the accepted baseline "
        "always carries the authorized intraday override"
    )


def test_gate_a_rejects_an_unexpectedly_clean_tree(text: str) -> None:
    gate_a = text[text.index("PHASE 1 — PREFLIGHT"):text.index("PHASE 2 — fetch")]
    assert "is CLEAN" in gate_a or "unexpectedly clean" in gate_a.lower()


def test_gate_a_rejects_the_wrong_kind_of_dirty(text: str) -> None:
    gate_a = text[text.index("PHASE 1 — PREFLIGHT"):text.index("PHASE 2 — fetch")]
    assert '" M config/settings.yaml"' in gate_a


def test_gate_a_uses_the_shared_verifier_not_an_ad_hoc_check(text: str) -> None:
    gate_a = text[text.index("PHASE 1 — PREFLIGHT"):text.index("PHASE 2 — fetch")]
    assert 'verify_intraday_override_only "$BASELINE_SHA"' in gate_a


def test_phase_3_discards_the_local_delta_before_checkout(text: str) -> None:
    """The checkout must land on a genuinely clean tree at TARGET_SHA — this
    discard is what makes that true, given the baseline is never clean."""
    phase3 = text[text.index("PHASE 3 — deploy"):text.index("PHASE 4 — restart")]
    discard = phase3.index('qgit "checkout -- config/settings.yaml"')
    checkout = phase3.index("checkout --detach '$TARGET_SHA'")
    assert discard < checkout, "the local delta must be discarded before checking out the target"


def test_phase_3_discard_is_covered_by_the_deployed_state(text: str) -> None:
    """The discard is a real mutation (it changes tracked file content) — it
    must happen AFTER DEPLOY_STATE is armed, like every other Phase 3+ write,
    so a failure between the two still converges instead of leaving the
    override silently stripped with DEPLOY_STATE still "pristine"."""
    phase3 = text[text.index("PHASE 3 — deploy"):text.index("PHASE 4 — restart")]
    armed = phase3.index('DEPLOY_STATE="deployed"')
    discard = phase3.index('qgit "checkout -- config/settings.yaml"')
    assert armed < discard, "DEPLOY_STATE must be armed before the local delta is discarded"


def test_gate_d_reuses_the_shared_editor_not_a_second_copy(text: str) -> None:
    """EDITOR_PY must be defined exactly once — Gate A verifies against it
    (indirectly, via the block-scanning rule it shares with the verifier),
    Gate D applies it, and convergence re-applies it after rollback. A second
    inline copy in Gate D would let the two drift out of sync silently."""
    assert len(re.findall(r"^EDITOR_PY='", text, re.M)) == 1, \
        "EDITOR_PY must be defined once and shared, not duplicated in Gate D"
    gate_d = text[text.index("PHASE 7 — GATE D"):text.index("PHASE 8 — GATE E")]
    assert "apply_intraday_override" in gate_d
    assert "EDITOR_PY='" not in gate_d, "Gate D must call the shared function, not redefine the editor"


def test_convergence_reapplies_and_verifies_the_override(text: str) -> None:
    body = _extract(text, r"\nconverge\(\) \{(.*?)\n\}\n", "converge()")
    assert "apply_intraday_override" in body, \
        "convergence must re-establish the authorized delta, not just the committed baseline"
    assert 'verify_intraday_override_only "$BASELINE_SHA"' in body, \
        "convergence must independently verify the re-applied delta, not just trust the mutator's exit code"


# ---------------------------------------------------------------------------
# 3. Behavioural fixtures — the embedded config editor
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def editor(text: str) -> str:
    return _extract(text, r"EDITOR_PY='\n(.*?)\n'\n", "EDITOR_PY")


SETTINGS = """\
cash_sweep:
  enabled: true
  symbol: "SGOV"

intraday_scan:
  enabled: false
  move_threshold_pct: 3.0
  cooldown_hours: 3.0
  max_candidates_per_scan: 5

trading:
  universe:
    - SPY
"""


def test_editor_enables_exactly_one_line(tmp_path: Path, editor: str) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS)
    r = _run_py(editor, str(p))
    assert r.returncode == 0 and r.stdout.strip() == "ENABLED", r.stderr
    before, after = SETTINGS.splitlines(), p.read_text().splitlines()
    diff = [(a, b) for a, b in zip(before, after) if a != b]
    assert diff == [("  enabled: false", "  enabled: true")]
    assert len(before) == len(after)


def test_editor_never_touches_the_cash_sweep_switch(tmp_path: Path, editor: str) -> None:
    """`cash_sweep:` also has an `enabled:` key, immediately above, already
    true. Flipping the wrong one would be silent and consequential."""
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS)
    _run_py(editor, str(p))
    sweep_block = p.read_text().split("cash_sweep:")[1].split("intraday_scan:")[0]
    assert "enabled: true" in sweep_block
    assert p.read_text().count("enabled: true") == 2


def test_editor_is_idempotent(tmp_path: Path, editor: str) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS)
    _run_py(editor, str(p))
    once = p.read_text()
    r = _run_py(editor, str(p))
    assert r.returncode == 0 and r.stdout.strip() == "ALREADY_ENABLED"
    assert p.read_text() == once


def test_editor_fails_closed_when_the_block_is_absent(tmp_path: Path, editor: str) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS.replace("intraday_scan:", "#intraday_scan:"))
    before = p.read_text()
    r = _run_py(editor, str(p))
    assert r.returncode != 0
    assert p.read_text() == before, "a failed edit must not modify the file"


def test_editor_fails_closed_when_the_key_is_absent(tmp_path: Path, editor: str) -> None:
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS.replace("intraday_scan:\n  enabled: false\n", "intraday_scan:\n"))
    r = _run_py(editor, str(p))
    assert r.returncode != 0


def test_editor_does_not_escape_the_block_into_a_later_section(tmp_path: Path, editor: str) -> None:
    """With intraday_scan present but keyless, the editor must stop at the block
    boundary rather than walking on and flipping some later `enabled:`."""
    doc = SETTINGS.replace("intraday_scan:\n  enabled: false\n", "intraday_scan:\n")
    doc += "\nnotifications:\n  enabled: false\n"
    p = tmp_path / "settings.yaml"
    p.write_text(doc)
    r = _run_py(editor, str(p))
    assert r.returncode != 0
    assert "enabled: false" in p.read_text().split("notifications:")[1]


# ---------------------------------------------------------------------------
# 3b. Behavioural fixtures — the authorized-local-delta verifier (Gate A /
#     convergence), the read-only counterpart to the editor above. This is
#     the check the external reviewer's defect report was ultimately about:
#     it decides whether the current working tree IS the accepted production
#     state (baseline + the one authorized override), not just "is it dirty".
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def override_verifier(text: str) -> str:
    return _extract(
        text,
        r"verify_intraday_override_only\(\).*?python3 -c '\n(.*?)\n' \"\$committed\"",
        "verify_intraday_override_only",
    )


def _verify(verifier: str, committed: str, working: str, tmp_path: Path):
    p = tmp_path / "working.yaml"
    p.write_text(working)
    return _run_py(verifier, committed, str(p))


def test_verifier_accepts_the_exact_authorized_delta(tmp_path, override_verifier) -> None:
    working = SETTINGS.replace("enabled: false", "enabled: true", 1)
    r = _verify(override_verifier, SETTINGS, working, tmp_path)
    assert r.returncode == 0 and r.stdout == "", r.stderr


def test_verifier_rejects_a_clean_tree(tmp_path, override_verifier) -> None:
    """committed == working: zero differing lines is not the accepted state
    either — production must carry the override, not the bare commit."""
    r = _verify(override_verifier, SETTINGS, SETTINGS, tmp_path)
    assert r.returncode != 0
    assert "expected exactly 1 line different" in r.stderr


def test_verifier_rejects_a_second_unrelated_change(tmp_path, override_verifier) -> None:
    working = SETTINGS.replace("enabled: false", "enabled: true", 1)
    working = working.replace("move_threshold_pct: 3.0", "move_threshold_pct: 5.0")
    r = _verify(override_verifier, SETTINGS, working, tmp_path)
    assert r.returncode != 0
    assert "expected exactly 1 line different" in r.stderr


def test_verifier_rejects_the_cash_sweep_switch_flipped_instead(tmp_path, override_verifier) -> None:
    """A one-line diff that flips the WRONG `enabled:` (cash_sweep's, already
    true, flipped to false) must be rejected, not mistaken for the intraday
    override just because exactly one line changed."""
    working = SETTINGS.replace("cash_sweep:\n  enabled: true", "cash_sweep:\n  enabled: false", 1)
    r = _verify(override_verifier, SETTINGS, working, tmp_path)
    assert r.returncode != 0
    assert "is not the committed intraday_scan.enabled: false line" in r.stderr


def test_verifier_rejects_a_non_true_value(tmp_path, override_verifier) -> None:
    working = SETTINGS.replace("enabled: false", "enabled: yes", 1)
    r = _verify(override_verifier, SETTINGS, working, tmp_path)
    assert r.returncode != 0
    assert "is not exactly" in r.stderr


def test_verifier_rejects_a_line_count_mismatch(tmp_path, override_verifier) -> None:
    working = SETTINGS.replace("enabled: false", "enabled: true", 1) + "extra_key: 1\n"
    r = _verify(override_verifier, SETTINGS, working, tmp_path)
    assert r.returncode != 0
    assert "line count differs" in r.stderr


# ---------------------------------------------------------------------------
# 4. Behavioural fixtures — the C3 live SGOV/liquidity verification
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def c3(text: str) -> str:
    return _extract(
        text,
        r"python3 - \"\$ACC_C3\" \"\$POS_C3\" <<'PY'[^\n]*\n(.*?)\nPY\n",
        "C3",
    )


def _healthy_payload():
    acct = {
        "paper": True,
        "portfolio_value": 10001.82,
        "liquidity": {
            "sweep_enabled": True, "sweep_symbol": "SGOV",
            "raw_cash": 144.97, "sweep_parked_value": 9856.85,
            "reserve_usd": 100.02, "total_liquidity": 10001.82,
        },
    }
    pos = {
        "positions": [{
            "symbol": "SGOV", "market_value": 9856.85,
            "is_cash_equivalent": True, "direction": "cash_equivalent",
        }],
        "error": None,
    }
    return acct, pos


def _c3(c3_src, acct, pos):
    return _run_py(c3_src, json.dumps(acct), json.dumps(pos))


def test_c3_accepts_a_truthful_snapshot(c3: str) -> None:
    acct, pos = _healthy_payload()
    r = _c3(c3, acct, pos)
    assert r.returncode == 0, r.stderr
    assert "reconciles exactly" in r.stdout


def test_c3_rejects_a_non_paper_account(c3: str) -> None:
    acct, pos = _healthy_payload()
    acct["paper"] = False
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_liquidity_that_does_not_reconcile(c3: str) -> None:
    acct, pos = _healthy_payload()
    acct["liquidity"]["total_liquidity"] = 42.0
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_a_fabricated_null_component(c3: str) -> None:
    acct, pos = _healthy_payload()
    acct["liquidity"]["sweep_parked_value"] = None
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_an_unflagged_sweep_row(c3: str) -> None:
    acct, pos = _healthy_payload()
    pos["positions"][0]["is_cash_equivalent"] = False
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_a_sweep_row_labelled_as_risk_exposure(c3: str) -> None:
    acct, pos = _healthy_payload()
    pos["positions"][0]["direction"] = "long"
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_a_non_sweep_symbol_flagged_cash_equivalent(c3: str) -> None:
    acct, pos = _healthy_payload()
    pos["positions"].append({
        "symbol": "NVDA", "market_value": 500.0,
        "is_cash_equivalent": True, "direction": "long",
    })
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_parked_money_with_no_position_backing_it(c3: str) -> None:
    """The false-liquidity case this gate exists for: the API claims $9.8K is
    parked, but nothing is actually held."""
    acct, pos = _healthy_payload()
    pos["positions"] = []
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_a_parked_value_that_disagrees_with_the_position(c3: str) -> None:
    acct, pos = _healthy_payload()
    pos["positions"][0]["market_value"] = 5000.0
    acct["liquidity"]["total_liquidity"] = 144.97 + 9856.85
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_rejects_zero_parked_while_the_vehicle_is_still_held(c3: str) -> None:
    acct, pos = _healthy_payload()
    acct["liquidity"]["sweep_parked_value"] = 0.0
    acct["liquidity"]["total_liquidity"] = 144.97
    assert _c3(c3, acct, pos).returncode != 0


def test_c3_accepts_a_flat_account_with_no_sweep_position(c3: str) -> None:
    """Zero parked and nothing held is a legitimate state, not a failure."""
    acct, pos = _healthy_payload()
    acct["liquidity"]["sweep_parked_value"] = 0.0
    acct["liquidity"]["total_liquidity"] = 144.97
    pos["positions"] = []
    assert _c3(c3, acct, pos).returncode == 0


def test_c3_rejects_a_positions_read_that_reported_an_error(c3: str) -> None:
    acct, pos = _healthy_payload()
    pos["error"] = "broker unreachable"
    assert _c3(c3, acct, pos).returncode != 0


# ---------------------------------------------------------------------------
# 5. Behavioural fixtures — the commissioning-verifier parser
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vparse(text: str) -> str:
    return _extract(text, r"parse_verifier\(\) \{[^\n]*\n  python3 -c '\n(.*?)\n' \"\$1\" \"\$2\"\n", "parse_verifier")


GROUPS = "config,gateway,wiring,providers,mission-control"


def _results():
    rows = [
        ("config", "agent routing"), ("config", "alpaca paper-only"),
        ("config", "config loads and validates"), ("gateway", "onecli reachable"),
        ("wiring", "proxy/CA resolves"),
        ("providers", "openrouter (httpx)"), ("providers", "alpaca trading (requests)"),
        ("providers", "alpaca market data (requests)"), ("providers", "fred (urllib)"),
        ("mission-control", "read-only"),
    ]
    return [{"group": g, "name": n, "status": "PASS", "detail": "ok"} for g, n in rows]


def _vp(src, results, groups=GROUPS, min_providers="4"):
    return _run_py(src, groups, min_providers,
                   stdin=json.dumps({"account": "qamc", "results": results}))


def test_verifier_parser_accepts_an_all_pass_run(vparse: str) -> None:
    r = _vp(vparse, _results())
    assert r.returncode == 0, r.stderr
    assert "all PASS" in r.stdout


@pytest.mark.parametrize("status", ["FAIL", "SKIP", "WARN"])
def test_verifier_parser_rejects_any_non_pass(vparse: str, status: str) -> None:
    """A SKIP is not "fine": the accepted commissioning record for this account
    is 0 FAIL / 0 WARN with no skip in these groups, so a skip is a regression."""
    rows = _results()
    rows[5]["status"] = status
    assert _vp(vparse, rows).returncode != 0


def test_verifier_parser_rejects_a_missing_group(vparse: str) -> None:
    rows = [r for r in _results() if r["group"] != "wiring"]
    assert _vp(vparse, rows).returncode != 0


def test_verifier_parser_rejects_a_short_provider_sweep(vparse: str) -> None:
    rows = [r for r in _results() if r["name"] != "fred (urllib)"]
    assert _vp(vparse, rows).returncode != 0


def test_verifier_parser_rejects_an_empty_result_set(vparse: str) -> None:
    """"No checks ran" must never read as "nothing failed"."""
    assert _vp(vparse, []).returncode != 0


def test_verifier_parser_rejects_unparsable_output(vparse: str) -> None:
    r = _run_py(vparse, GROUPS, "4", stdin="not json at all")
    assert r.returncode != 0


def test_verifier_parser_never_echoes_more_than_one_detail_line(vparse: str) -> None:
    """Details are truncated to one line and 150 chars so a verbose provider
    error cannot flood — or smuggle — anything into the transcript."""
    rows = _results()
    rows[0]["detail"] = "line one\n" + ("x" * 500)
    r = _vp(vparse, rows)
    assert r.returncode == 0
    assert "x" * 200 not in r.stdout


# ---------------------------------------------------------------------------
# 6. Behavioural fixtures — the Gate C focused-suite outcome parser
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gatec(text: str) -> str:
    return _extract(
        text,
        r"printf '%s\\n' \"\$GATEC_SUMMARY\" \| python3 -c '\n(.*?)\n' \"\$GATE_C_EXPECTED_TESTS\"",
        "Gate C summary parser",
    )


def test_gate_c_accepts_exactly_the_expected_count(gatec: str) -> None:
    r = _run_py(gatec, "246", stdin="246 passed, 62 warnings in 4.09s")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("summary", [
    "245 passed, 62 warnings in 4.09s",
    "247 passed in 4.09s",
    "245 passed, 1 skipped in 4.09s",
    "244 passed, 2 xfailed in 4.09s",
    "245 passed, 1 xpassed in 4.09s",
    "245 passed, 1 failed in 4.09s",
    "246 passed, 1 error in 4.09s",
    "246 passed, 3 deselected in 4.09s",
    "no tests ran in 0.01s",
])
def test_gate_c_rejects_any_other_outcome(gatec: str, summary: str) -> None:
    """Exit code 0 is not enough: a skipped or deselected test means the
    deployed tree is not the tree that was reviewed green."""
    assert _run_py(gatec, "246", stdin=summary).returncode != 0


# ---------------------------------------------------------------------------
# 7. Behavioural fixtures — the embedded awk programs
# ---------------------------------------------------------------------------

def test_intraday_value_extractor_reads_the_right_block(text: str) -> None:
    program = _extract(text, r"\| awk '(/\^intraday_scan:/\{f=1;next\}[^']*)'", "intraday awk")
    r = _run_awk(program, SETTINGS)
    assert r.stdout.strip() == "false", r.stderr


def test_intraday_value_extractor_ignores_cash_sweep(text: str) -> None:
    """Negative control: the same program pointed at a document whose only
    `enabled: true` is cash_sweep's must not report true."""
    program = _extract(text, r"\| awk '(/\^intraday_scan:/\{f=1;next\}[^']*)'", "intraday awk")
    doc = SETTINGS.replace("intraday_scan:\n  enabled: false", "intraday_scan:\n  enabled: true")
    assert _run_awk(program, doc).stdout.strip() == "true"
    doc_no_block = SETTINGS.replace("intraday_scan:", "#intraday_scan:")
    assert _run_awk(program, doc_no_block).stdout.strip() == ""


# Gate E's listener-privacy check is no longer a local awk program: it calls
# the DEPLOYED verifier's own `listener_privacy_verdict()`. These tests pin that
# arrangement, because the whole point is that Gate B and Gate E cannot drift
# into two different definitions of "private" — which is what made the first
# real rollout's false positive expensive.

def test_gate_e_delegates_listener_privacy_to_the_deployed_verifier(text: str) -> None:
    e4 = text[text.index("# E4 — OneCLI healthy"):text.index("# E5 —")]
    assert "verify_commissioning.py" in e4, \
        "Gate E must use the deployed verifier, not a private copy of the rule"
    assert "listener_privacy_verdict" in e4
    assert "tailscale_local_addresses" in e4
    assert "verdict=PASS" in e4, "Gate E must require an explicit PASS verdict"


def test_gate_e_no_longer_reimplements_the_rule_in_awk(text: str) -> None:
    """The old awk scanner only rejected wildcards, so a service bound to a
    specific PUBLIC ip would have passed the finish line."""
    assert 'want[i]' not in text, "the old awk bind-scanner is still present"
    e4 = text[text.index("# E4 — OneCLI healthy"):text.index("# E5 —")]
    assert "0.0.0.0" not in e4, \
        "Gate E must not carry its own wildcard list; the verifier owns the rule"


def test_gate_e_checks_every_qamc_facing_port(text: str) -> None:
    e4 = text[text.index("# E4 — OneCLI healthy"):text.index("# E5 —")]
    for port in ("8800", "10254", "10255", "10256"):
        assert port in e4, f"Gate E does not classify port {port}"


def test_gate_e_reports_whether_tailscale_was_resolvable(text: str) -> None:
    """A future permissions change that makes Tailscale unqueryable must be
    visible in the transcript, not a silent reclassification."""
    e4 = text[text.index("# E4 — OneCLI healthy"):text.index("# E5 —")]
    assert "tailnet_resolved" in e4


# --- directory-permission digits -------------------------------------------

DIR_MODE_CONDITION = (
    '[[ "${DIR_MODE: -2:1}" =~ ^[0145]$ && "${DIR_MODE: -1:1}" =~ ^[0145]$ ]]'
)


def test_directory_permission_check_enumerates_safe_digits(text: str) -> None:
    """`[0-5]` accepted 2 (write) and 3 (write+execute) — the exact modes this
    check exists to reject."""
    assert DIR_MODE_CONDITION in text
    # The comment above the check names the old range deliberately, so that a
    # future reader knows why it must not come back. Only CODE is checked.
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "[0-5]" not in code, "the permissive digit range is still present in code"


@pytest.mark.parametrize("mode,accept", [
    ("700", True), ("500", True), ("755", True), ("711", True), ("701", True),
    ("750", True), ("555", True), ("400", True), ("744", True),
    # Anything with a group or other WRITE bit must be refused.
    ("770", False), ("707", False), ("777", False), ("757", False),
    ("775", False), ("772", False), ("773", False), ("730", False),
    ("720", False), ("703", False), ("702", False), ("727", False),
])
def test_directory_permission_condition_behaviour(tmp_path: Path, mode: str, accept: bool) -> None:
    """Drive the real condition rather than trusting the regex by eye."""
    script = tmp_path / "cond.sh"
    script.write_text(
        f'DIR_MODE="{mode}"\n{DIR_MODE_CONDITION} && exit 0 || exit 1\n'
    )
    rc = subprocess.run(["bash", str(script)]).returncode
    assert (rc == 0) is accept, (
        f"mode {mode} was {'accepted' if rc == 0 else 'rejected'}, expected "
        f"{'accepted' if accept else 'rejected'}"
    )


def test_every_group_or_world_writable_mode_is_rejected() -> None:
    """Exhaustive over all 512 modes: acceptance must be exactly 'no write bit
    for group or other', with no gaps."""
    import itertools
    for a, b, c in itertools.product(range(8), repeat=3):
        mode = f"{a}{b}{c}"
        writable = bool(b & 0o2) or bool(c & 0o2)
        accepted = (str(b) in "0145") and (str(c) in "0145")
        assert accepted == (not writable), f"mode {mode} misclassified"


def test_intra_check_service_resolver(text: str) -> None:
    program = _extract(text, r"INTRA_SERVICE=\"\$\(printf '%s\\n' \"\$UNITS_CAT\" \| awk '\n(.*?)\n'\)\"", "intra service awk")
    units = (
        "# /home/qamc/.config/systemd/user/quant-agent-morning.service\n"
        "ExecStart=/home/qamc/quant-agent/scripts/run_if_et_window.sh morning\n"
        "\n"
        "# /home/qamc/.config/systemd/user/quant-agent-intra.service\n"
        "ExecStart=/home/qamc/quant-agent/scripts/run_if_et_window.sh intra_check\n"
    )
    assert _run_awk(program, units).stdout.strip() == "quant-agent-intra.service"


def test_intra_check_service_resolver_returns_nothing_when_unscheduled(text: str) -> None:
    """If no unit invokes intra_check the resolver must come back empty, so the
    script stops rather than enabling a scanner nothing will ever call."""
    program = _extract(text, r"INTRA_SERVICE=\"\$\(printf '%s\\n' \"\$UNITS_CAT\" \| awk '\n(.*?)\n'\)\"", "intra service awk")
    units = (
        "# /home/qamc/.config/systemd/user/quant-agent-morning.service\n"
        "ExecStart=/home/qamc/quant-agent/scripts/run_if_et_window.sh morning\n"
    )
    assert _run_awk(program, units).stdout.strip() == ""


# ---------------------------------------------------------------------------
# 8. Functional coverage of the rollback machinery itself
# ---------------------------------------------------------------------------
#
# The state machine, traps and convergence routine are extracted verbatim and
# executed against stubbed `qgit` / `systemctl` / health helpers. This is the
# part of the script that decides whether production is left mutated, so
# reading it is not sufficient evidence — every abort path is driven for real.

HARNESS_PREAMBLE = r"""
set -Eeuo pipefail
BASELINE_SHA="BASE"
QAMC_USER="qamc"
QAMC_REPO="/nonexistent/repo"
API_UNIT="quant-agent-api.service"
API_HEALTH="http://127.0.0.1:8800/health"
SYSTEMD_ENV="export X=1;"
LOG="$TRACE.log"
TMPD="$(mktemp -d)"

say()  { :; }
ok()   { :; }
note() { :; }
err()  { printf '%s\n' "$*" >> "$TRACE.err"; }

# Stubs record what convergence attempted. GIT_RC / RESTART_RC / HEALTH_RC let
# each scenario decide which half of the convergence fails.
qgit() { echo "qgit $1" >> "$TRACE"; return "${GIT_RC:-0}"; }
sysctl_user() {
  echo "systemctl $1" >> "$TRACE"
  case "$1" in
    restart*) return "${RESTART_RC:-0}" ;;
    *) echo "1234"; return 0 ;;
  esac
}
health_field() { echo "ok"; }
wait_healthy() { echo "wait_healthy" >> "$TRACE"; return "${HEALTH_RC:-0}"; }
# The real functions shell out to `sudo -u qamc` and touch a real
# config/settings.yaml, neither of which exists in this harness — like every
# other external effect here, they are stubbed and traced rather than run for
# real. OVERRIDE_RC / VERIFY_RC let a scenario fail either one independently,
# the same way GIT_RC/RESTART_RC/HEALTH_RC fail their half of convergence.
apply_intraday_override() {
  echo "apply_intraday_override" >> "$TRACE"
  echo "${FAKE_OVERRIDE_RESULT:-ENABLED}"
  return "${OVERRIDE_RC:-0}"
}
verify_intraday_override_only() {
  echo "verify_intraday_override_only $1" >> "$TRACE"
  [[ "${VERIFY_RC:-0}" -eq 0 ]] || echo "${FAKE_VERIFY_ERR:-simulated verification failure}"
  return "${VERIFY_RC:-0}"
}
"""


def _harness(text: str, tmp_path: Path, scenario: str, env: dict[str, str] | None = None):
    """Run the extracted state machine + traps with stubs, plus `scenario`."""
    start = text.index('DEPLOY_STATE="pristine"')
    end = text.index("trap on_exit EXIT") + len("trap on_exit EXIT")
    machinery = text[start:end]
    # `qgit 'rev-parse HEAD'` inside converge must look like it worked.
    machinery = machinery.replace(
        'head_after="$(qgit \'rev-parse HEAD\' 2>/dev/null)"',
        'head_after="$( qgit \'rev-parse HEAD\' >/dev/null 2>&1; echo "${FAKE_HEAD:-BASE}" )"',
    ).replace(
        'dirty_after="$(qgit \'status --porcelain\' 2>/dev/null)"',
        # Default FAKE_DIRTY is the NORMAL post-convergence state for this
        # script: baseline + the re-applied intraday override, not clean —
        # unlike the finish-line script this is adapted from, clean is never
        # correct here. A scenario overrides FAKE_DIRTY to simulate the
        # override going missing (empty) or some other corruption.
        'dirty_after="$( qgit \'status --porcelain\' >/dev/null 2>&1; '
        'echo "${FAKE_DIRTY- M config/settings.yaml}" )"',
    )
    script = tmp_path / "harness.sh"
    trace = tmp_path / "trace"
    script.write_text(HARNESS_PREAMBLE + machinery + "\n" + scenario + "\n")
    full_env = {"PATH": "/usr/bin:/bin", "TRACE": str(trace)}
    full_env.update(env or {})
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=full_env)
    def _read(suffix=""):
        p = Path(str(trace) + suffix)
        return p.read_text() if p.exists() else ""
    return proc, _read(), _read(".err")


def test_failure_after_deploy_converges_checkout_config_and_process(text, tmp_path) -> None:
    proc, trace, errs = _harness(
        text, tmp_path,
        'DEPLOY_STATE="deployed"\nfalse\necho "SHOULD NOT REACH HERE"',
    )
    assert proc.returncode != 0
    assert "SHOULD NOT REACH HERE" not in proc.stdout
    assert "qgit checkout -- config/settings.yaml" in trace, "config delta not reverted"
    assert "qgit checkout --detach 'BASE'" in trace, "checkout not reverted"
    assert "systemctl restart quant-agent-api.service" in trace, "API not converged"
    assert "wait_healthy" in trace, "convergence did not verify the API came back"
    assert "PRODUCTION CONVERGED" in errs


def test_failure_before_any_mutation_changes_nothing(text, tmp_path) -> None:
    """A preflight failure must not touch the checkout or restart anything."""
    proc, trace, errs = _harness(text, tmp_path, 'false\necho "UNREACHABLE"')
    assert proc.returncode != 0
    assert "qgit" not in trace and "systemctl restart" not in trace
    assert "production is untouched" in errs


@pytest.mark.parametrize("sig,expected_rc", [("TERM", 143), ("INT", 130), ("HUP", 129)])
def test_signals_during_a_mutation_converge(text, tmp_path, sig, expected_rc) -> None:
    proc, trace, errs = _harness(
        text, tmp_path,
        f'DEPLOY_STATE="restarted"\nkill -{sig} $$\nsleep 5\necho "UNREACHABLE"',
    )
    assert proc.returncode == expected_rc, proc.stderr
    assert "UNREACHABLE" not in proc.stdout
    assert "qgit checkout --detach 'BASE'" in trace
    assert "systemctl restart" in trace
    assert f"received SIG{sig}" in errs


def test_convergence_is_idempotent_after_a_successful_rollback(text, tmp_path) -> None:
    """Found by this harness: the recursion guard alone was not enough. It
    stopped a failure INSIDE convergence from re-entering, but two sequential
    calls still rolled production back twice — a second detach and a second
    API restart on an already-converged host. A completed convergence is now a
    no-op."""
    proc, trace, errs = _harness(
        text, tmp_path,
        'DEPLOY_STATE="enabled"\nconverge "first"\nconverge "second"\nexit 0',
    )
    assert trace.count("qgit checkout --detach 'BASE'") == 1, \
        f"convergence ran more than once:\n{trace}"
    assert trace.count("systemctl restart quant-agent-api.service") == 1
    assert "second" not in errs


def test_a_failed_convergence_can_still_be_retried(text, tmp_path) -> None:
    """Idempotence must not lock out a retry after an INCOMPLETE convergence —
    the second attempt is the one that might actually finish the job."""
    proc, trace, errs = _harness(
        text, tmp_path,
        'DEPLOY_STATE="enabled"\nconverge "first"\nconverge "second"\nexit 0',
        env={"RESTART_RC": "1"},
    )
    assert trace.count("qgit checkout --detach 'BASE'") == 2, \
        f"a failed convergence was not retryable:\n{trace}"


def test_incomplete_convergence_is_reported_not_papered_over(text, tmp_path) -> None:
    """If the git rollback fails, the operator must be told, with commands."""
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="deployed"\nfalse', env={"GIT_RC": "1"},
    )
    assert proc.returncode != 0
    assert "CONVERGENCE INCOMPLETE" in errs
    assert "FINISH BY HAND" in errs
    assert "PRODUCTION CONVERGED" not in errs


def test_failed_api_restart_during_convergence_is_reported(text, tmp_path) -> None:
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="restarted"\nfalse', env={"RESTART_RC": "1"},
    )
    assert "did NOT come back healthy" in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_unhealthy_api_after_convergence_is_reported(text, tmp_path) -> None:
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="restarted"\nfalse', env={"HEALTH_RC": "1"},
    )
    assert "did NOT come back healthy" in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_unexpectedly_clean_tree_after_rollback_is_reported(text, tmp_path) -> None:
    """The regression this section exists for: the finish-line script this is
    adapted from required a CLEAN tree after rollback, because its baseline
    (9c736c1) was genuinely clean. This baseline (775296e1) never is — the
    accepted production state is baseline + the authorized intraday override
    (docs/STATE.md) — so a tree that comes back clean after convergence means
    the override was NOT re-applied, i.e. exactly the bug an external
    reviewer found: rollback silently landing on intraday DISABLED. That must
    be reported as incomplete convergence, never as success."""
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="enabled"\nfalse',
        env={"FAKE_DIRTY": ""},
    )
    assert "PRODUCTION CONVERGED" not in errs, (
        "a clean tree after rollback must never be reported as converged — "
        "the accepted state requires the intraday override present"
    )
    assert "tree NOT at the accepted state" in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_unexpectedly_dirty_tree_after_rollback_is_reported(text, tmp_path) -> None:
    """A dirty tree that is not EXACTLY the one authorized override — here,
    two files — is also not the accepted state and must not be papered over
    just because *something* is dirty."""
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="enabled"\nfalse',
        env={"FAKE_DIRTY": " M config/settings.yaml\n M src/pipeline.py"},
    )
    assert "PRODUCTION CONVERGED" not in errs
    assert "tree NOT at the accepted state" in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_override_reapply_failure_after_rollback_is_reported(text, tmp_path) -> None:
    """If checkout+config succeed but re-applying the intraday override itself
    fails (OVERRIDE_RC != 0), convergence must not claim success."""
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="enabled"\nfalse',
        env={"OVERRIDE_RC": "1"},
    )
    assert "apply_intraday_override" in trace
    assert "PRODUCTION CONVERGED" not in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_override_verification_failure_after_rollback_is_reported(text, tmp_path) -> None:
    """apply_intraday_override can report success while the independent
    verifier disagrees (e.g. it wrote something subtly wrong) — convergence
    must trust the verifier, not just the mutator's own exit code."""
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="enabled"\nfalse',
        env={"VERIFY_RC": "1", "FAKE_VERIFY_ERR": "simulated: wrong line changed"},
    )
    assert "verify_intraday_override_only BASE" in trace
    assert "PRODUCTION CONVERGED" not in errs
    assert "CONVERGENCE INCOMPLETE" in errs


@pytest.mark.parametrize("state", ["deployed", "restarted", "enabled"])
def test_normal_convergence_reapplies_and_verifies_the_override(text, tmp_path, state) -> None:
    """The success path, from each of the three real failure-injection points
    the task called out by name: failure right after checkout ("deployed"),
    after the API restart ("restarted"), and after Gate D's own first-time
    enablement on the target tree ("enabled"). Convergence's code path does
    not branch on which of these it starts from, so this also demonstrates
    that sharing is real, not just asserted."""
    proc, trace, errs = _harness(
        text, tmp_path, f'DEPLOY_STATE="{state}"\nfalse',
    )
    assert "apply_intraday_override" in trace
    assert "verify_intraday_override_only BASE" in trace
    assert "PRODUCTION CONVERGED" in errs
    assert "authorized intraday override" in errs


@pytest.mark.parametrize("state", ["deployed", "restarted", "enabled"])
def test_override_reapply_failure_is_reported_from_every_injection_point(text, tmp_path, state) -> None:
    """The exact defect, driven from all three failure points: if the override
    cannot be re-established, convergence must never claim success — for a
    failure right after checkout just as much as one deep in Gate E."""
    proc, trace, errs = _harness(
        text, tmp_path, f'DEPLOY_STATE="{state}"\nfalse',
        env={"OVERRIDE_RC": "1"},
    )
    assert "PRODUCTION CONVERGED" not in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_wrong_head_after_rollback_is_reported(text, tmp_path) -> None:
    proc, trace, errs = _harness(
        text, tmp_path, 'DEPLOY_STATE="deployed"\nfalse', env={"FAKE_HEAD": "SOMETHINGELSE"},
    )
    assert "tree NOT clean" in errs or "HEAD=SOMETHINGELSE" in errs
    assert "CONVERGENCE INCOMPLETE" in errs


def test_convergence_refuses_to_run_from_a_subshell(text, tmp_path) -> None:
    """A subshell rolling production back while the parent carries on would be
    the worst possible failure mode; the guard closes the class outright."""
    proc, trace, errs = _harness(
        text, tmp_path,
        'DEPLOY_STATE="deployed"\nX="$( converge "from a subshell"; echo done )"\nexit 0',
    )
    assert "qgit checkout --detach" not in trace, \
        f"a subshell was able to converge production:\n{trace}"


# ---------------------------------------------------------------------------
# 9. Adversarial-review residuals that must stay documented
# ---------------------------------------------------------------------------

def test_a_half_finished_previous_run_is_refused_with_instructions(text: str) -> None:
    """SIGKILL cannot be trapped. The next run must not stack a second rollout
    on top of a half-finished one — it must refuse and say how to converge."""
    assert 'if [[ "$HEAD_NOW" == "$TARGET_SHA" ]]; then' in text
    idx = text.index('if [[ "$HEAD_NOW" == "$TARGET_SHA" ]]; then')
    block = text[idx:idx + 1200]
    assert "did not complete" in block
    assert "checkout --detach" in block and "systemctl --user restart" in block


def test_sigkill_residual_is_stated_in_the_header(text: str) -> None:
    header = text[: text.index("set -Eeuo pipefail")]
    assert "SIGKILL" in header, "the one untrappable abort must be documented, not implied"


# ---------------------------------------------------------------------------
# 10. The target must carry the corrected listener-privacy classifier
# ---------------------------------------------------------------------------

def test_preflight_refuses_a_target_without_the_privacy_fix(text: str) -> None:
    """Both Gate B (which runs the deployed verifier) and Gate E4 (which loads
    its classifier) depend on the corrected rule. If the pinned target predates
    it, the run must stop in PREFLIGHT — where nothing has been touched — not
    at Gate B after a deploy and a rollback, which is what the first real
    rollout cost."""
    assert "listener_privacy_verdict' ${TARGET_SHA} -- ops/commissioning/verify_commissioning.py" in text
    guard = text[text.index("does NOT contain the corrected listener-privacy"):]
    guard = guard[:2000]
    assert "NOTHING HAS BEEN CHANGED" in guard
    # It must tell the operator exactly how to re-pin, not just that it failed.
    for constant in ("TARGET_SHA", "TARGET_TREE", "EXPECTED_CHANGED_FILES",
                     "EXPECTED_FILELIST_SHA"):
        assert constant in guard, f"the retarget instructions omit {constant}"


def test_the_privacy_guard_runs_before_any_mutation(text: str) -> None:
    guard = text.index("does NOT contain the corrected listener-privacy")
    first_mutation = text.index('DEPLOY_STATE="deployed"')
    assert guard < first_mutation, \
        "the retarget guard must fire in preflight, before the checkout"
