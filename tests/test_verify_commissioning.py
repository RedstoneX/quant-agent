"""Offline tests for ops/commissioning/verify_commissioning.py.

The script's value is that its verdicts are trustworthy — a commissioning
gate that rounds ambiguous evidence up to PASS is worse than no gate. These
tests pin the decision logic (which is pure and network-free) so a later
edit cannot quietly turn a FAIL into a PASS.

The network probes and subprocess-backed checks are deliberately NOT
exercised here; they are integration surface, and the suite is hermetic
(see tests/conftest.py, which disables outbound HTTP).

Loaded via importlib because `ops/` is a runbook/tooling directory, not an
installed package — same pattern as tests/test_export_alpaca_trades.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "ops" / "commissioning" / "verify_commissioning.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_commissioning", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations via
    # sys.modules[cls.__module__], which is None for an unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vc = _load_module()


# --- redact_proxy: the agent token must never reach output -----------------

def test_redact_proxy_hides_the_agent_token():
    proxy = "http://x:aoc_supersecrettokenvalue@127.0.0.1:10255"
    out = vc.redact_proxy(proxy)
    assert "aoc_supersecrettokenvalue" not in out
    assert out == "http://***@127.0.0.1:10255"


def test_redact_proxy_handles_tokenless_and_empty_values():
    assert vc.redact_proxy("http://127.0.0.1:10255") == "http://127.0.0.1:10255"
    assert vc.redact_proxy(None) == "(unset)"
    assert vc.redact_proxy("") == "(unset)"


def test_redact_proxy_keeps_only_the_last_at_sign_segment():
    # A token containing an '@' must not leave a fragment of itself behind.
    out = vc.redact_proxy("http://x:tok@en@127.0.0.1:10255")
    assert "tok" not in out
    assert out == "http://***@127.0.0.1:10255"


# --- classify_injection: the direct-vs-gateway proof ----------------------

def test_injection_proven_when_direct_rejects_and_gateway_accepts():
    status, detail = vc.classify_injection(401, 200)
    assert status == vc.PASS
    assert "401" in detail and "200" in detail


@pytest.mark.parametrize("direct", [400, 401, 403])
def test_injection_fails_when_gateway_also_rejects(direct):
    status, detail = vc.classify_injection(direct, 401)
    assert status == vc.FAIL
    assert "did not inject" in detail


def test_injection_fails_when_the_direct_leg_already_succeeds():
    """A fake credential accepted WITHOUT the gateway proves nothing.

    Either the probe endpoint does not validate credentials, or a real
    credential is present client-side — the exact thing this whole
    architecture exists to prevent. Must never read as a pass.
    """
    status, detail = vc.classify_injection(200, 200)
    assert status == vc.FAIL
    assert "does not validate credentials" in detail


def test_injection_skips_when_a_leg_is_unevaluable():
    assert vc.classify_injection(None, 200)[0] == vc.SKIP
    assert vc.classify_injection(401, None)[0] == vc.SKIP


# --- check_agent_routing: every seat on the model the policy gives it -----

def _roster(**overrides):
    """A roster that matches EXPECTED_ROUTING exactly — built FROM the policy
    map so these tests keep testing the check rather than a frozen copy of
    whichever models the policy happened to name when they were written."""
    out = []
    for name, model in vc.EXPECTED_ROUTING.items():
        entry = {
            "agent_name": name,
            "configured_provider": vc.EXPECTED_PROVIDER,
            "configured_model": model,
        }
        entry.update(overrides.get(name, {}))
        out.append(entry)
    return out


def test_routing_passes_when_all_nine_agents_match():
    status, detail = vc.check_agent_routing(_roster())
    assert status == vc.PASS
    assert "all 9 agents" in detail


def test_routing_fails_when_a_seat_runs_another_seats_model():
    """The per-seat check's reason for existing: under a multi-model policy,
    "provider is openrouter and the model is one we use" is true of a
    mis-wired seat too. Swapping a specialist's cheap model onto the risk
    manager must fail."""
    specialists = [
        m for a, m in vc.EXPECTED_ROUTING.items()
        if m != vc.EXPECTED_ROUTING["risk_manager"]
    ]
    if not specialists:  # a single-model policy has nothing to swap
        pytest.skip("policy currently routes every seat to one model")
    status, detail = vc.check_agent_routing(
        _roster(risk_manager={"configured_model": specialists[0]})
    )
    assert status == vc.FAIL
    assert "risk_manager" in detail


def test_routing_fails_when_a_seat_is_missing_from_the_roster():
    roster = [e for e in _roster() if e["agent_name"] != "meta_reflector"]
    status, detail = vc.check_agent_routing(roster)
    assert status == vc.FAIL
    assert "meta_reflector" in detail


def test_routing_fails_on_a_single_drifted_model():
    status, detail = vc.check_agent_routing(
        _roster(risk_manager={"configured_model": "openai/gpt-4o"})
    )
    assert status == vc.FAIL
    assert "risk_manager" in detail


def test_routing_fails_when_a_provider_is_left_to_prefix_inference():
    """`None` provider means "infer from the model-id prefix" (src/config.py).

    For an OpenRouter "vendor/model" id that inference picks the wrong
    provider, so an unset provider is a real misconfiguration here, not a
    harmless default.
    """
    status, detail = vc.check_agent_routing(
        _roster(macro_analyst={"configured_provider": None})
    )
    assert status == vc.FAIL
    assert "macro_analyst=inferred" in detail


def test_routing_fails_on_an_empty_roster():
    assert vc.check_agent_routing([])[0] == vc.FAIL


# --- looks_like_placeholder ----------------------------------------------

@pytest.mark.parametrize("value", [
    "", "   ", "placeholder", "PLACEHOLDER", "changeme", "your-key-here",
    "managed-by-onecli", "xxx-not-real-xxx",
])
def test_recognized_placeholders(value):
    assert vc.looks_like_placeholder(value) is True


@pytest.mark.parametrize("value", [
    "sk-or-v1-abcdef0123456789abcdef0123456789",
    "PKTESTKEYID0123456789",
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
])
def test_real_looking_values_are_not_treated_as_placeholders(value):
    assert vc.looks_like_placeholder(value) is False


# --- parse_health: broker_reachable is the commissioning signal -----------

def _verdicts(**overrides):
    body = {
        "status": "ok", "db_reachable": True, "paper": True,
        "broker_reachable": True,
    }
    body.update(overrides)
    return {name: (status, detail) for name, status, detail in vc.parse_health(body)}


def test_health_all_green_passes_every_verdict():
    verdicts = _verdicts()
    assert {s for s, _ in verdicts.values()} == {vc.PASS}


def test_health_fails_when_broker_is_unreachable():
    status, detail = _verdicts(broker_reachable=False)["broker reachable"]
    assert status == vc.FAIL
    assert "credentials are configured but the broker call failed" in detail


def test_health_fails_and_says_so_when_broker_is_unconfigured():
    """`None` is distinct from `False` — not configured vs. configured-but-down.

    This is the state the deployment is in until step 4 of
    ops/onecli/README.md is applied, so the message must point there.
    """
    status, detail = _verdicts(broker_reachable=None)["broker reachable"]
    assert status == vc.FAIL
    assert "not configured" in detail
    assert "ops/onecli/README.md" in detail


def test_health_fails_when_paper_mode_is_off():
    """Live trading is not authorized — `paper` anything but True fails."""
    assert _verdicts(paper=False)["alpaca paper mode"][0] == vc.FAIL
    assert _verdicts(paper=None)["alpaca paper mode"][0] == vc.FAIL


def test_health_fails_when_db_is_unreachable():
    assert _verdicts(db_reachable=False)["db reachable"][0] == vc.FAIL


def test_health_fails_on_a_degraded_status_field():
    assert _verdicts(status="degraded")["api responds"][0] == vc.FAIL


# --- is_missing_credentials_error: the dev-vs-runtime distinction ---------

def test_missing_credentials_error_is_recognized():
    exc = ValueError("Required API key 'alpaca_key' is empty — check your .env file")
    assert vc.is_missing_credentials_error(exc) is True


def test_other_config_errors_are_not_excused():
    """Only the "this account holds no credentials" case may downgrade to SKIP.

    A genuinely broken config (bad provider, malformed YAML) must stay a
    hard FAIL wherever it is run.
    """
    exc = ValueError("Invalid provider 'openrouterr'; must be one of [...]")
    assert vc.is_missing_credentials_error(exc) is False


# --- exit-code contract ---------------------------------------------------

def test_main_exits_nonzero_when_any_check_fails(monkeypatch, capsys):
    monkeypatch.setitem(
        vc.GROUPS, "config",
        lambda ctx: ctx.add("config", "synthetic", vc.FAIL, "forced"),
    )
    assert vc.main(["--group", "config", "--no-network"]) == 1
    assert "COMMISSIONING ACCEPTANCE: FAIL" in capsys.readouterr().out


def test_main_exits_zero_when_checks_only_skip(monkeypatch, capsys):
    """SKIP must never fail the run — it means "not evaluable here"."""
    monkeypatch.setitem(
        vc.GROUPS, "config",
        lambda ctx: ctx.add("config", "synthetic", vc.SKIP, "not evaluable"),
    )
    assert vc.main(["--group", "config", "--no-network"]) == 0
    out = capsys.readouterr().out
    assert "COMMISSIONING ACCEPTANCE: PASS" in out
    assert "review them before accepting" in out


def test_json_output_is_machine_readable(monkeypatch, capsys):
    import json

    monkeypatch.setitem(
        vc.GROUPS, "config",
        lambda ctx: ctx.add("config", "synthetic", vc.PASS, "ok"),
    )
    vc.main(["--group", "config", "--no-network", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["results"][0]["name"] == "synthetic"


# --- --live preflight gating ---------------------------------------------
#
# The preflight group makes authenticated calls with real credentials and
# spends money on one LLM completion. It must therefore be genuinely
# opt-in: every path that could run it without `--live` is pinned here.


def _ctx(**kwargs):
    return vc.Ctx(**kwargs)


def test_preflight_is_skipped_unless_live_is_requested():
    ctx = _ctx(allow_network=True, live=False, proxy="http://x:t@127.0.0.1:10255")
    vc.check_preflight(ctx)
    assert [r.status for r in ctx.results] == [vc.SKIP]
    assert "--live" in ctx.results[0].detail


def test_preflight_is_skipped_with_no_network_even_when_live():
    ctx = _ctx(allow_network=False, live=True, proxy="http://x:t@127.0.0.1:10255")
    vc.check_preflight(ctx)
    assert [r.status for r in ctx.results] == [vc.SKIP]


def test_preflight_is_skipped_when_no_gateway_wiring_resolved():
    """Without the gateway there are no credentials to call with — running
    anyway would hit the providers unauthenticated and report noise."""
    ctx = _ctx(allow_network=True, live=True, proxy=None)
    vc.check_preflight(ctx)
    assert [r.status for r in ctx.results] == [vc.SKIP]


def test_live_flag_defaults_off_on_the_context():
    assert vc.Ctx().live is False


def test_main_does_not_enable_live_by_default(monkeypatch):
    """A bare invocation must never spend money or make authenticated calls."""
    seen = {}
    monkeypatch.setitem(
        vc.GROUPS, "preflight",
        lambda ctx: seen.update(live=ctx.live),
    )
    vc.main(["--group", "preflight", "--no-network"])
    assert seen == {"live": False}


def test_main_passes_the_live_flag_through(monkeypatch):
    seen = {}
    monkeypatch.setitem(
        vc.GROUPS, "preflight",
        lambda ctx: seen.update(live=ctx.live),
    )
    vc.main(["--group", "preflight", "--no-network", "--live"])
    assert seen == {"live": True}


# --- credential selection for the preflight -------------------------------

def test_preflight_credentials_fall_back_to_a_placeholder(monkeypatch):
    """On `dev` the credential env vars are deliberately empty, so
    get_config() raises. The gateway substitutes the real value anyway, so
    a placeholder still exercises the true end-to-end path."""
    import src.api.deps as deps

    def _raise():
        raise ValueError("Required API key 'alpaca_key' is empty — check your .env file")

    monkeypatch.setattr(deps, "get_config", _raise)
    assert vc._credentials_for_preflight() == (vc.PREFLIGHT_PLACEHOLDER,) * 4


def test_preflight_prefers_the_runtime_configuration(monkeypatch):
    """From the runtime account the preflight must prove the EXACT
    production path, not an approximation of it."""
    import src.api.deps as deps
    from types import SimpleNamespace

    monkeypatch.setattr(deps, "get_config", lambda: SimpleNamespace(
        api_keys=SimpleNamespace(
            openrouter="or-value", alpaca_key="ak-value",
            alpaca_secret="as-value", fred="fred-value",
        ),
    ))
    assert vc._credentials_for_preflight() == (
        "or-value", "ak-value", "as-value", "fred-value",
    )


# --- account coverage -----------------------------------------------------
#
# Full acceptance spans two accounts by design: the runtime account owns the
# checks needing real credentials, its own systemd session and its
# environment; the isolation check is only meaningful from OFF the runtime
# account. A single-account run is therefore PARTIAL, and must say so — an
# operator reading a green summary as full coverage is exactly the
# misreading the split invites.


def _r(group, name, status, resolved_by=None):
    return vc.Result(group=group, name=name, status=status, detail="",
                     resolved_by=resolved_by)


def test_coverage_is_complete_when_nothing_waits_on_another_account():
    note = vc.coverage_note([_r("config", "a", vc.PASS)], "qamc")
    assert note.startswith("ACCOUNT COVERAGE: complete")
    assert "'qamc'" in note


def test_coverage_names_the_account_that_resolves_each_pending_check():
    results = [
        _r("config", "config loads and validates", vc.SKIP, resolved_by="qamc"),
        _r("safety", "trading timers disabled", vc.SKIP, resolved_by="qamc"),
        _r("gateway", "onecli gateway listening", vc.PASS),
    ]
    note = vc.coverage_note(results, "dev")
    assert "partial" in note
    assert "2 check(s) need another account" in note
    assert "as qamc:" in note
    assert "config/config loads and validates" in note
    assert "safety/trading timers disabled" in note


def test_coverage_ignores_checks_this_account_already_resolved():
    """A SKIP tagged with the CURRENT account is not pending — it skipped
    for some other reason and must not be reported as waiting on itself."""
    results = [_r("isolation", "runtime credentials are unreadable off-account",
                  vc.SKIP, resolved_by="dev")]
    assert vc.coverage_note(results, "dev").startswith("ACCOUNT COVERAGE: complete")


def test_coverage_reports_the_isolation_check_as_pending_from_the_runtime():
    """Run from `qamc`, the isolation check proves nothing — it needs an
    account that should NOT be able to read the runtime's home."""
    results = [_r("isolation", "runtime credentials are unreadable off-account",
                  vc.SKIP, resolved_by="dev")]
    note = vc.coverage_note(results, "qamc")
    assert "as dev:" in note


def test_coverage_groups_multiple_pending_accounts():
    results = [
        _r("safety", "trading timers disabled", vc.SKIP, resolved_by="qamc"),
        _r("isolation", "unreadable off-account", vc.SKIP, resolved_by="dev"),
    ]
    note = vc.coverage_note(results, "ubuntu")
    assert "as dev:" in note and "as qamc:" in note


def test_coverage_ignores_untagged_skips():
    """`--no-network` skips are not account-bound and must not be reported
    as waiting on another login."""
    results = [_r("providers", "credential injection", vc.SKIP)]
    assert vc.coverage_note(results, "dev").startswith("ACCOUNT COVERAGE: complete")


def test_render_appends_the_coverage_note(capsys):
    out = vc.render([_r("safety", "trading timers disabled", vc.SKIP,
                        resolved_by="qamc")], account="dev")
    assert "COMMISSIONING ACCEPTANCE" in out
    assert "ACCOUNT COVERAGE: partial" in out


def test_json_output_carries_the_account_and_pending_list(monkeypatch, capsys):
    import json

    monkeypatch.setitem(
        vc.GROUPS, "safety",
        lambda ctx: ctx.add("safety", "trading timers disabled", vc.SKIP,
                            "no session", resolved_by="qamc"),
    )
    vc.main(["--group", "safety", "--no-network", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["account"]
    assert payload["pending_accounts"] == ["qamc"] or payload["account"] == "qamc"
