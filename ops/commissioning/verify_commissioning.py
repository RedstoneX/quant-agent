#!/usr/bin/env python3
"""Automated QAMC commissioning acceptance check.

Turns the manual pass/fail list in `docs/WORK.md` ("Verification before
commissioning checkpoint") into a single command with a deterministic exit
code, so accepting the commissioned deployment is an evidence run rather
than a re-derived sequence of ad-hoc `curl` invocations.

    python ops/commissioning/verify_commissioning.py            # all groups
    python ops/commissioning/verify_commissioning.py --from-onecli
    python ops/commissioning/verify_commissioning.py --live      # + real reads
    python ops/commissioning/verify_commissioning.py --group config --json

Exit code is 0 when nothing FAILed and 1 otherwise. SKIP never fails the
run: a check that cannot be evaluated from the current account (e.g.
trading-timer state, which only `qamc` can see) reports SKIP with the
reason, so the same script is honest run from `dev`, `qamc`, or `ubuntu`
and gets progressively more complete as it moves closer to the runtime.

## Secret discipline

This script is read-only and never prints a credential, and it never
submits, modifies, or cancels an order.

The `providers` group deliberately sends an obviously-fake placeholder
credential and compares only HTTP **status codes** between a direct call
and a gateway-routed call — the same discipline used throughout the manual
commissioning evidence in `docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`.
Its response bodies are never read (every request is streamed and closed),
so no injected credential or account data enters this process at all.

The opt-in `preflight` group (`--live`) necessarily *does* read responses —
proving the configured model answers, or that a bar series parses, means
looking at the answer. It reports shape and verdicts only: never a
credential, never an account balance.

## Why each provider is probed with a different HTTP library

`CREDENTIAL_DELIVERY_EVIDENCE.md` records that the three stacks QAMC
actually uses do not agree on how CA trust is configured — `requests`
honors `REQUESTS_CA_BUNDLE` but *not* `SSL_CERT_FILE`. Probing every
provider with one convenient library would verify a transport QAMC never
uses and would silently miss exactly that class of misconfiguration. So
each provider is probed through the same library its real caller uses:

    OpenRouter  -> httpx     (via the `openai` SDK, src/agents/base.py)
    Alpaca      -> requests  (via `alpaca-py`, src/execution/broker.py)
    FRED        -> urllib    (via `fredapi`, src/data/macro.py)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# OneCLI's own two ports, per the upstream compose file (see ops/onecli/README.md).
ONECLI_DASHBOARD_PORT = 10254
ONECLI_GATEWAY_PORT = 10255
# Mission Control's default bind (see .env.example / docs/architecture/MISSION_CONTROL_API.md).
MISSION_CONTROL_PORT = int(os.environ.get("QUANT_AGENT_API_PORT", "8800"))

LOOPBACK = "127.0.0.1"

# The accepted routing posture (docs/architecture/MODEL_ROUTING_POLICY.md):
# every agent explicitly on OpenRouter, each seat pinned to the model the
# benchmark selected for it.
#
# This map is deliberately a SEPARATE copy of the policy from
# `config/settings.yaml`, not a read of it. The check's whole value is
# proving that what is deployed equals what was reviewed — a verifier that
# sourced its expectations from the file it is verifying would pass on any
# config, including one edited in place on the runtime host.
EXPECTED_PROVIDER = "openrouter"
EXPECTED_ROUTING: dict[str, str] = {
    "tech_analyst":      "google/gemini-2.5-flash-lite",
    "news_analyst":      "google/gemini-2.5-flash-lite",
    "macro_analyst":     "google/gemini-2.5-flash-lite",
    "earnings_analyst":  "google/gemini-2.5-flash-lite",
    "portfolio_manager": "openai/gpt-5.5",
    # Deliberately NOT PM's model — the AI Risk Manager is the gate over the
    # Portfolio Manager, and measured quality at that seat is tied, so the
    # policy buys decision-chain independence rather than spending the tie on
    # nothing. See config/settings.yaml and MODEL_ROUTING_POLICY.md.
    "risk_manager":      "qwen/qwen3-235b-a22b-2507",
    "position_reviewer": "google/gemini-2.5-flash-lite",
    "evening_analyst":   "google/gemini-2.5-flash-lite",
    "meta_reflector":    "google/gemini-2.5-flash-lite",
}

# Obviously-fake credential. Its only job is to be rejected by a direct
# call, so that a 2xx through the gateway can only mean the gateway
# substituted a real value server-side.
FAKE_CREDENTIAL = "qamc-commissioning-probe-not-a-real-key"

# Stand-in used by the live preflight when this account holds no runtime
# credentials (i.e. on `dev`). The gateway substitutes the real value, so
# the call still exercises the true end-to-end path.
PREFLIGHT_PLACEHOLDER = "placeholder-managed-by-onecli"

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


@dataclass
class Result:
    group: str
    name: str
    status: str
    detail: str = ""
    resolved_by: str | None = None
    """Account that CAN evaluate this check, when the current one cannot.

    Acceptance is legitimately split across accounts — that is the account
    boundary working, not a limitation to route around. Recording which
    account resolves each SKIP is what lets an operator tell a partial run
    from a complete one instead of reading a green summary as full
    coverage.
    """


@dataclass
class Ctx:
    """Everything the checks share: resolved gateway wiring plus knobs."""

    proxy: str | None = None
    ca_bundle: str | None = None
    proxy_source: str = "unset"
    allow_network: bool = True
    live: bool = False
    results: list[Result] = field(default_factory=list)

    def add(self, group: str, name: str, status: str, detail: str = "",
            resolved_by: str | None = None) -> Result:
        r = Result(group, name, status, detail, resolved_by)
        self.results.append(r)
        return r


# --------------------------------------------------------------------------
# Pure helpers (unit-tested offline — see tests/test_verify_commissioning.py)
# --------------------------------------------------------------------------


def redact_proxy(proxy: str | None) -> str:
    """Render a proxy URL with its agent token replaced by `***`.

    The gateway proxy URL embeds OneCLI's per-agent token as HTTP basic-auth
    userinfo. That token is not a provider credential, but it *is* the
    capability to have real credentials injected — it must never reach
    stdout, a log, or committed evidence.
    """
    if not proxy:
        return "(unset)"
    if "@" not in proxy:
        return proxy
    scheme, _, rest = proxy.partition("://")
    if not rest:
        return "***"
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}" if scheme else f"***@{hostpart}"


def classify_injection(direct_status: int | None, gateway_status: int | None) -> tuple[str, str]:
    """Decide whether a provider's credential injection is proven.

    The proof is a *difference*, not an absolute code: the same fake
    credential must be rejected when sent directly and accepted when sent
    through the gateway. Both legs succeeding, or both failing, proves
    nothing and is reported as such rather than being rounded up to a pass.
    """
    if direct_status is None or gateway_status is None:
        return SKIP, "one leg could not be evaluated"
    if 200 <= direct_status < 300:
        # The fake credential was ACCEPTED without the gateway. Either the
        # endpoint does not actually validate credentials (so it can't prove
        # anything) or a real credential leaked into the client environment.
        return FAIL, (
            f"direct call with a fake credential returned {direct_status} — "
            "this endpoint does not validate credentials, or a real "
            "credential is present client-side; proof is invalid"
        )
    if not 200 <= gateway_status < 300:
        return FAIL, (
            f"direct={direct_status} gateway={gateway_status} — the gateway did "
            "not inject a working credential (check the secret's grant, host "
            "pattern, and value format in OneCLI)"
        )
    return PASS, f"direct={direct_status} (rejected) -> gateway={gateway_status} (accepted)"


def check_agent_routing(roster: Iterable[dict]) -> tuple[str, str]:
    """Verify every agent is pinned to the model the accepted policy gives it.

    Per-seat rather than one global model: the policy routes specialists to
    cheap models and the decision seats to stronger ones, so "everything on
    one id" is no longer the correct posture — and a check that only asserted
    "provider is openrouter" would let any model through, including one the
    benchmark rejected.
    """
    wrong: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for entry in roster:
        name = (entry.get("agent_name") or "").strip()
        provider = (entry.get("configured_provider") or "").strip().lower()
        model = (entry.get("configured_model") or "").strip()
        seen.add(name)
        expected = EXPECTED_ROUTING.get(name)
        if expected is None:
            unknown.append(f"{name}={provider or 'inferred'}/{model or 'unset'}")
            continue
        if provider != EXPECTED_PROVIDER or model != expected:
            wrong.append(
                f"{name}={provider or 'inferred'}/{model or 'unset'} "
                f"(expected {EXPECTED_PROVIDER}/{expected})"
            )
    if not seen:
        return FAIL, "agent roster is empty"
    missing = sorted(set(EXPECTED_ROUTING) - seen)
    problems = wrong + [f"unknown agent {u}" for u in unknown]
    if missing:
        problems.append(f"absent from roster: {', '.join(missing)}")
    if problems:
        return FAIL, (
            f"{len(problems)} routing deviation(s) from the accepted policy: "
            + "; ".join(problems)
        )
    distinct = sorted(set(EXPECTED_ROUTING.values()))
    return PASS, (
        f"all {len(seen)} agents on {EXPECTED_PROVIDER}, per-seat models match "
        f"the accepted policy ({len(distinct)} distinct: {', '.join(distinct)})"
    )


def looks_like_placeholder(value: str) -> bool:
    """True when a credential-shaped config value is clearly NOT a real key.

    QAMC's own `.env` must hold placeholders only — the real values live in
    OneCLI. This is deliberately conservative: it answers "is this
    recognizably a placeholder", and anything it cannot vouch for is
    reported as a WARN for a human to look at, never silently passed.
    """
    v = (value or "").strip().lower()
    if not v:
        return True
    placeholder_markers = (
        "placeholder", "changeme", "change-me", "dummy", "fake", "example",
        "your-", "your_", "xxx", "todo", "notreal", "not-real", "unused",
        "managed-by-onecli", "onecli",
    )
    return any(marker in v for marker in placeholder_markers)


def parse_health(payload: dict) -> list[tuple[str, str, str]]:
    """Turn a `/health` body into named commissioning verdicts.

    `broker_reachable` is the objective commissioning signal: `None` means
    "not configured", `False` means "configured but the call failed", and
    only `True` means the whole credential chain is live.
    """
    out: list[tuple[str, str, str]] = []

    status = payload.get("status")
    out.append((
        "api responds",
        PASS if status == "ok" else FAIL,
        f"status={status!r}",
    ))

    db = payload.get("db_reachable")
    out.append((
        "db reachable",
        PASS if db is True else FAIL,
        f"db_reachable={db!r}",
    ))

    paper = payload.get("paper")
    out.append((
        "alpaca paper mode",
        PASS if paper is True else FAIL,
        f"paper={paper!r} (live trading is not authorized)",
    ))

    broker = payload.get("broker_reachable")
    if broker is True:
        out.append(("broker reachable", PASS, "broker_reachable=True — credential chain is live"))
    elif broker is False:
        out.append((
            "broker reachable", FAIL,
            "broker_reachable=False — credentials are configured but the broker "
            "call failed (gateway down, or wiring incorrect)",
        ))
    else:
        out.append((
            "broker reachable", FAIL,
            "broker_reachable=None — Alpaca credentials are not configured in the "
            "runtime yet; apply step 4 of ops/onecli/README.md",
        ))
    return out


# Unit-file STATE values that mean "this timer will not be started by
# systemd on its own". Anything NOT in this set — including a value this
# script does not recognise — is treated as enabled, because the check it
# feeds is a safety gate and an unreadable state is not evidence of safety.
#
# `static` and `indirect` are deliberately absent. `static` units have no
# [Install] section and cannot be enabled directly, and `indirect` means
# disabled-but-pullable-by-another-unit; for a TRADING timer both are worth
# a human look rather than a silent pass.
_TIMER_STATES_NOT_ENABLED = frozenset({
    "disabled",
    "masked",
    "masked-runtime",
    "bad",
    "not-found",
})


def enabled_timer_units(list_unit_files_stdout: str) -> list[str]:
    """Timer units that `systemctl --user list-unit-files` reports as enabled.

    Parses the STATE **column**, not the line. `list-unit-files` emits three
    whitespace-separated columns — UNIT FILE, STATE, PRESET — so a correctly
    disabled timer whose vendor preset is `enabled` prints:

        quant-agent-morning.timer disabled enabled

    A substring search for "enabled" matches that line and reports a disabled
    timer as enabled. That is the bug this function exists to prevent: it
    turned a clean runtime into a FAIL during commissioning acceptance and,
    being a false alarm on a safety gate, is exactly the kind of noise that
    teaches an operator to skim past the gate.

    Fail-closed in the other direction too. A line whose STATE column is
    missing or unrecognised is RETURNED as enabled rather than dropped —
    "this script could not read the state" must never render as "the timer
    is off". Only the states in `_TIMER_STATES_NOT_ENABLED` clear it.

    Expects `--no-legend` output (the caller passes it). A header line
    survives harmlessly: "UNIT FILE" does not end in `.timer`.
    """
    enabled: list[str] = []
    for line in (list_unit_files_stdout or "").splitlines():
        fields = line.split()
        if not fields or not fields[0].endswith(".timer"):
            continue
        state = fields[1].lower() if len(fields) > 1 else ""
        if state not in _TIMER_STATES_NOT_ENABLED:
            enabled.append(fields[0])
    return enabled


# --------------------------------------------------------------------------
# Network probes — each streams and closes, never reading a response body
# --------------------------------------------------------------------------


def _probe_httpx(url: str, headers: dict, proxy: str | None, ca: str | None) -> int:
    import httpx

    kwargs: dict = {"timeout": 20.0, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy
    if ca:
        kwargs["verify"] = ca
    with httpx.Client(**kwargs) as client:
        with client.stream("GET", url, headers=headers) as resp:
            return resp.status_code


def _probe_requests(url: str, headers: dict, proxy: str | None, ca: str | None) -> int:
    import requests

    session = requests.Session()
    session.trust_env = False  # explicit legs only — never ambient env
    try:
        resp = session.get(
            url,
            headers=headers,
            timeout=20,
            stream=True,  # body is never read into memory
            proxies={"https": proxy, "http": proxy} if proxy else {},
            verify=ca if ca else True,
        )
        try:
            return resp.status_code
        finally:
            resp.close()
    finally:
        session.close()


def _probe_urllib(url: str, headers: dict, proxy: str | None, ca: str | None) -> int:
    import ssl
    import urllib.error
    import urllib.request

    handlers: list = [urllib.request.ProxyHandler({"https": proxy} if proxy else {})]
    if ca:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = opener.open(req, timeout=20)
        try:
            return resp.getcode()
        finally:
            resp.close()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code
        finally:
            exc.close()


# Each provider: (label, url, fake headers/params, probe fn matching the
# real caller's HTTP stack). Endpoints are GET-only and chosen because they
# actually validate the credential — an endpoint that answers regardless of
# auth would make the direct-vs-gateway comparison meaningless.
PROVIDER_PROBES: list[tuple[str, str, dict, Callable]] = [
    (
        "openrouter (httpx)",
        "https://openrouter.ai/api/v1/auth/key",
        {"Authorization": f"Bearer {FAKE_CREDENTIAL}"},
        _probe_httpx,
    ),
    (
        "alpaca trading (requests)",
        "https://paper-api.alpaca.markets/v2/account",
        {"APCA-API-KEY-ID": FAKE_CREDENTIAL, "APCA-API-SECRET-KEY": FAKE_CREDENTIAL},
        _probe_requests,
    ),
    (
        "alpaca market data (requests)",
        "https://data.alpaca.markets/v2/stocks/AAPL/trades/latest",
        {"APCA-API-KEY-ID": FAKE_CREDENTIAL, "APCA-API-SECRET-KEY": FAKE_CREDENTIAL},
        _probe_requests,
    ),
    (
        # FRED takes its credential as a query parameter, not a header —
        # OneCLI injects it the same way (see CREDENTIAL_DELIVERY_EVIDENCE.md).
        "fred (urllib)",
        f"https://api.stlouisfed.org/fred/series?series_id=GDP&file_type=json&api_key={FAKE_CREDENTIAL}",
        {},
        _probe_urllib,
    ),
]


# --------------------------------------------------------------------------
# Check groups
# --------------------------------------------------------------------------


def _port_bindings(port: int) -> list[str]:
    """Local addresses `port` is listening on, via `ss`. [] if unknown."""
    try:
        out = subprocess.run(
            ["ss", "-tln"], capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hosts: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, p = local.rpartition(":")
        if p == str(port):
            hosts.append(addr)
    return hosts


# --------------------------------------------------------------------------
# Listener-privacy classification
# --------------------------------------------------------------------------
#
# 2026-08-20: the first real finish-line rollout aborted at Gate B on a FALSE
# POSITIVE here. The rule was "loopback or it is public", so OneCLI's dashboard
# port failed the moment `tailscaled` served it on the tailnet — even though
# the operator evidence showed Docker publishing OneCLI only as
# 127.0.0.1:10254-10255, the extra listeners being `tailscaled` itself, and
# `tailscale serve status` reporting the listener as tailnet-only. Private
# Tailscale operator access is accepted QAMC architecture (docs/STATE.md), so
# equating "non-loopback" with "public" was simply wrong.
#
# The fix must not overshoot in the other direction. Whitelisting 100.64.0.0/10
# (Tailscale's CGNAT range), fd7a::/16, or RFC1918 would mean any process that
# binds a plausible-looking address passes the gate. So the ONLY non-loopback
# addresses accepted are the exact addresses Tailscale itself reports for THIS
# host. A CGNAT-shaped address Tailscale does not claim is foreign, and is
# treated exactly like a public one.


def normalize_listen_addr(raw: str) -> str:
    """Canonical form of a listener address for comparison.

    `ss` renders IPv6 in brackets and may carry a zone id; Tailscale renders
    the same address bare. Both are parsed through `ipaddress` so
    `[fd7a:115c:a1e0::9034:aa62]` and `fd7a:115c:a1e0:0:0:0:9034:aa62` compare
    equal rather than differing as strings.
    """
    text = (raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.strip("[]")
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.lower()


WILDCARD_ADDRS = {"0.0.0.0", "*", "::", "[::]", ""}


def is_loopback_addr(raw: str) -> bool:
    normalized = normalize_listen_addr(raw)
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_wildcard_addr(raw: str) -> bool:
    text = (raw or "").strip()
    if text in WILDCARD_ADDRS:
        return True
    normalized = normalize_listen_addr(text)
    if normalized in WILDCARD_ADDRS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_unspecified
    except ValueError:
        return False


_TAILNET_CACHE: list = []   # [] = not yet resolved; [value] = resolved (value may be None)


def tailscale_local_addresses_cached() -> set[str] | None:
    """`tailscale_local_addresses()` resolved at most once per run."""
    if not _TAILNET_CACHE:
        _TAILNET_CACHE.append(tailscale_local_addresses())
    return _TAILNET_CACHE[0]


_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _env_without_proxies() -> dict:
    """The current environment minus proxy variables.

    The runtime account sources OneCLI's proxy/CA wiring before running this
    script, and `tailscale` talks to a local unix socket. Handing it a proxy it
    must not use invites an environment-dependent failure that would look like
    "Tailscale is unavailable" and fail the privacy gate closed for the wrong
    reason.
    """
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_VARS}
    return env


def _tailscale_binary() -> str | None:
    """Resolve the `tailscale` CLI.

    PATH first, then the absolute locations it is actually installed to. The
    runtime account's non-login shell has a narrower PATH than an interactive
    one, and "the binary was not on PATH" must not be able to masquerade as
    "this host has no Tailscale addresses" — that is precisely how a listener
    that IS private would get failed closed for the wrong reason.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in ("/usr/bin/tailscale", "/usr/local/bin/tailscale",
                      "/usr/sbin/tailscale", "/opt/tailscale/tailscale"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def tailscale_local_addresses() -> set[str] | None:
    """This host's OWN Tailscale addresses, according to Tailscale.

    Returns a normalized set, or **None** when Tailscale cannot be
    interrogated. None means "unknown", never "none" — callers must fail
    closed on it rather than reading an unanswered question as an absence of
    tailnet addresses.
    """
    binary = _tailscale_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True, text=True, timeout=10, env=_env_without_proxies(),
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
            raw = (data.get("Self") or {}).get("TailscaleIPs") or []
            addrs = {
                normalize_listen_addr(ip) for ip in raw
                if isinstance(ip, str) and ip.strip()
            }
            if addrs:
                return addrs
        except (ValueError, AttributeError, TypeError):
            pass

    # Fallback for a tailscaled too old for --json, or a status shape change.
    addrs = set()
    saw_output = False
    for flag in ("-4", "-6"):
        try:
            proc = subprocess.run(
                [binary, "ip", flag],
                capture_output=True, text=True, timeout=10, env=_env_without_proxies(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for token in proc.stdout.split():
            normalized = normalize_listen_addr(token)
            try:
                ipaddress.ip_address(normalized)
            except ValueError:
                continue
            saw_output = True
            addrs.add(normalized)
    return addrs if saw_output and addrs else None


def classify_listener_addresses(
    bindings: Iterable[str], tailnet: set[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Split listener addresses into (loopback, tailnet, foreign).

    `tailnet` is this host's exact Tailscale addresses, or None if unknown.
    An address is tailnet-private only if it is EXACTLY one of those — never
    because it merely falls inside a private-looking range. Wildcards are
    always foreign; when `tailnet` is None every other non-loopback address is
    foreign too, so an undiscoverable Tailscale fails the gate closed instead
    of being waved through.
    """
    loopback: list[str] = []
    tailnet_hit: list[str] = []
    foreign: list[str] = []
    known = tailnet or set()
    for raw in bindings:
        if is_wildcard_addr(raw):
            foreign.append(raw)
        elif is_loopback_addr(raw):
            loopback.append(raw)
        elif normalize_listen_addr(raw) in known:
            tailnet_hit.append(raw)
        else:
            foreign.append(raw)
    return loopback, tailnet_hit, foreign


def listener_privacy_verdict(
    bindings: list[str], tailnet: set[str] | None,
) -> tuple[str, str]:
    """(status, detail) for one port's listener set. Pure; unit-tested."""
    if not bindings:
        return SKIP, "`ss` unavailable or port not listed"

    loopback, tailnet_hit, foreign = classify_listener_addresses(bindings, tailnet)

    if foreign:
        if tailnet is None and not all(is_wildcard_addr(b) for b in foreign):
            return FAIL, (
                f"cannot classify non-loopback address(es): {', '.join(foreign)} — "
                "this host's Tailscale addresses could not be determined "
                "(`tailscale status --json` / `tailscale ip` unavailable), so "
                "they cannot be confirmed as private tailnet listeners. "
                "Failing closed: an unanswered question is not a pass."
            )
        return FAIL, (
            f"bound to address(es) that are neither loopback nor this host's "
            f"Tailscale addresses: {', '.join(foreign)} — public exposure is "
            "not authorized"
        )

    parts = []
    if loopback:
        parts.append(f"loopback {', '.join(loopback)}")
    if tailnet_hit:
        parts.append(f"tailnet-only {', '.join(tailnet_hit)}")
    if not parts:
        return SKIP, "no classifiable listener addresses"
    return PASS, "bound " + " + ".join(parts)


def _tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_missing_credentials_error(exc: BaseException) -> bool:
    """True when a config-load failure is *only* "this account has no .env".

    `dev` deliberately holds no runtime credentials, so `${ALPACA_API_KEY}`
    and friends substitute to empty and validation fails. That is the
    account boundary working as designed, not a commissioning defect — it
    must read as SKIP from `dev` and still be a hard FAIL from the runtime
    account, where those values genuinely must resolve.
    """
    text = str(exc)
    return "is empty — check your .env file" in text or (
        "API key must be set" in text and "At least one of" in text
    )


def check_config(ctx: Ctx) -> None:
    """Config-layer checks. No network, no credentials — pure file reads."""
    group = "config"
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import yaml

        from src.api.deps import _config_path, agent_roster, get_config
    except Exception as exc:
        ctx.add(group, "config module imports", FAIL, f"{type(exc).__name__}: {exc}")
        return

    # Routing and paper-mode are facts about `config/settings.yaml` itself,
    # independent of whether this account can resolve the credential env
    # vars — so they are read from the raw YAML and stay checkable from any
    # account, including `dev`.
    #
    # `_config_path()` is relative by default, so resolve it against the repo
    # root rather than the caller's cwd — the operator runs this from a
    # runbook, not necessarily from the project directory.
    settings = _config_path()
    if not settings.is_absolute():
        settings = PROJECT_ROOT / settings
    try:
        raw = yaml.safe_load(settings.read_text()) or {}
    except Exception as exc:
        ctx.add(group, "settings.yaml readable", FAIL, f"{type(exc).__name__}: {exc}")
        return

    llm = raw.get("llm") or {}
    roster = [
        {
            "agent_name": name,
            "configured_provider": llm.get(f"{name}_provider"),
            "configured_model": llm.get(f"{name}_model"),
        }
        for name in (
            "tech_analyst", "news_analyst", "macro_analyst", "earnings_analyst",
            "portfolio_manager", "risk_manager", "position_reviewer",
            "evening_analyst", "meta_reflector",
        )
    ]
    status, detail = check_agent_routing(roster)
    ctx.add(group, "agent routing", status, detail)

    paper = (raw.get("alpaca") or {}).get("paper")
    ctx.add(
        group, "alpaca paper-only",
        PASS if paper is True else FAIL,
        f"alpaca.paper={paper!r}",
    )

    # Full validation — what the trading process itself does at startup.
    # `deps.get_config()` is lru_cached and resolves the same relative path,
    # so point it at the file already resolved above before its first call.
    os.environ.setdefault("QUANT_AGENT_API_CONFIG", str(settings))
    try:
        cfg = get_config()
    except Exception as exc:
        if is_missing_credentials_error(exc):
            ctx.add(group, "config loads and validates", SKIP,
                    "credential env vars are empty in this account — expected on "
                    "`dev`; re-run as the runtime account to check startup validation",
                    resolved_by=RUNTIME_ACCOUNT)
        else:
            # A config that will not load is the single most important early
            # failure: the trading process would die the same way at startup.
            ctx.add(group, "config loads and validates", FAIL,
                    f"{type(exc).__name__}: {exc}")
        return
    ctx.add(group, "config loads and validates", PASS, "startup validation passed")

    # Cross-check the validated object against the raw read, so a future
    # change to config parsing cannot make the cheap YAML checks above lie.
    status, detail = check_agent_routing(agent_roster())
    ctx.add(group, "agent routing (validated config)", status, detail)

    # QAMC's own config must hold placeholders only; the real values live in
    # OneCLI. A value this cannot recognize as a placeholder is a WARN, not a
    # pass — it may be a real key sitting where it should not be.
    unrecognized = []
    for label, value in (
        ("openrouter", cfg.api_keys.openrouter),
        ("alpaca_key", cfg.api_keys.alpaca_key),
        ("alpaca_secret", cfg.api_keys.alpaca_secret),
        ("fred", cfg.api_keys.fred),
    ):
        if not looks_like_placeholder(value):
            unrecognized.append(label)
    if unrecognized:
        ctx.add(
            group, "credentials are placeholders only", WARN,
            "could not confirm these are placeholders: "
            f"{', '.join(unrecognized)} — real values belong only in OneCLI",
        )
    else:
        ctx.add(group, "credentials are placeholders only", PASS,
                "no real-looking credential in config/.env")


def check_gateway(ctx: Ctx) -> None:
    """OneCLI is running, and it is private."""
    group = "gateway"
    for label, port in (("dashboard", ONECLI_DASHBOARD_PORT), ("gateway", ONECLI_GATEWAY_PORT)):
        reachable = _tcp_open(LOOPBACK, port)
        ctx.add(
            group, f"onecli {label} listening",
            PASS if reachable else FAIL,
            f"{LOOPBACK}:{port} {'accepting connections' if reachable else 'refused'}",
        )
        bindings = _port_bindings(port)
        status, detail = listener_privacy_verdict(
            bindings, tailscale_local_addresses_cached(),
        )
        ctx.add(group, f"onecli {label} is private", status, detail)


def check_wiring(ctx: Ctx) -> None:
    """The three env vars the runtime needs, and their CA file."""
    group = "wiring"
    if not ctx.allow_network:
        ctx.add(group, "gateway proxy configured", SKIP, "--no-network")
        return
    if not ctx.proxy:
        ctx.add(group, "gateway proxy configured", FAIL,
                "no HTTPS_PROXY in the environment and --from-onecli not used — "
                "apply step 4 of ops/onecli/README.md in the runtime's .env")
        return

    ctx.add(group, "gateway proxy configured", PASS,
            f"{redact_proxy(ctx.proxy)} (source: {ctx.proxy_source})")

    # `host.docker.internal` is what OneCLI's own container-config reports;
    # it resolves only inside a Docker container, never for QAMC's bare
    # `qamc`-account processes. Catching this here is cheaper than watching
    # every outbound call fail DNS at runtime.
    if "host.docker.internal" in ctx.proxy:
        ctx.add(group, "proxy host is reachable from a bare process", FAIL,
                "proxy points at host.docker.internal, which only resolves inside "
                f"a container — use {LOOPBACK}")
    else:
        ctx.add(group, "proxy host is reachable from a bare process", PASS,
                "not host.docker.internal")

    if ctx.ca_bundle:
        ca = Path(ctx.ca_bundle)
        if not ca.is_file():
            ctx.add(group, "gateway CA bundle readable", FAIL, f"{ca} is not a readable file")
        else:
            try:
                head = ca.read_text(errors="replace")[:64]
            except OSError as exc:
                ctx.add(group, "gateway CA bundle readable", FAIL, f"{ca}: {exc}")
            else:
                ok = "BEGIN CERTIFICATE" in head
                ctx.add(group, "gateway CA bundle readable",
                        PASS if ok else FAIL,
                        f"{ca} {'is a PEM certificate' if ok else 'is not PEM-formatted'}")
    else:
        ctx.add(group, "gateway CA bundle readable", FAIL,
                "no CA bundle resolved (SSL_CERT_FILE / REQUESTS_CA_BUNDLE)")

    # requests (Alpaca's transport) does not honor SSL_CERT_FILE — both vars
    # must be set in the real runtime environment or Alpaca alone will fail
    # TLS verification against the gateway while OpenRouter and FRED work.
    if ctx.proxy_source == "environment":
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            present = bool(os.environ.get(var))
            ctx.add(group, f"{var} set", PASS if present else FAIL,
                    "set" if present else
                    f"{var} is unset — see docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md")
    else:
        ctx.add(group, "runtime CA env vars set", SKIP,
                "wiring was resolved from OneCLI, not from this process's "
                "environment — run this as the runtime account to check them",
                resolved_by=RUNTIME_ACCOUNT)


def check_providers(ctx: Ctx) -> None:
    """Prove credential injection per provider, through the real HTTP stack."""
    group = "providers"
    if not ctx.allow_network:
        ctx.add(group, "credential injection", SKIP, "--no-network")
        return
    if not ctx.proxy:
        ctx.add(group, "credential injection", SKIP, "no gateway wiring resolved")
        return

    for label, url, headers, probe in PROVIDER_PROBES:
        try:
            direct = probe(url, headers, None, None)
        except Exception as exc:
            ctx.add(group, label, SKIP, f"direct leg failed: {type(exc).__name__}: {exc}")
            continue
        try:
            gateway = probe(url, headers, ctx.proxy, ctx.ca_bundle)
        except Exception as exc:
            ctx.add(group, label, FAIL,
                    f"direct={direct} but the gateway leg raised "
                    f"{type(exc).__name__}: {exc}")
            continue
        status, detail = classify_injection(direct, gateway)
        ctx.add(group, label, status, detail)


def check_mission_control(ctx: Ctx) -> None:
    """Mission Control is up, private, read-only, and reporting honestly."""
    group = "mission-control"
    if not ctx.allow_network:
        ctx.add(group, "health", SKIP, "--no-network")
        return

    bindings = _port_bindings(MISSION_CONTROL_PORT)
    status, detail = listener_privacy_verdict(
        bindings, tailscale_local_addresses_cached(),
    )
    ctx.add(group, "api is private", status, detail)

    url = f"http://{LOOPBACK}:{MISSION_CONTROL_PORT}/health"
    try:
        import urllib.request

        # Deliberately proxy-free: Mission Control is loopback, and routing a
        # loopback call through the credential gateway would be nonsense.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        ctx.add(group, "health", FAIL, f"GET /health failed: {type(exc).__name__}: {exc}")
        return

    for name, status, detail in parse_health(payload):
        ctx.add(group, name, status, detail)


def _credentials_for_preflight() -> tuple[str, str, str, str]:
    """(openrouter, alpaca_key, alpaca_secret, fred) for the live preflight.

    Prefer the runtime's real configuration when it loads, so the preflight
    proves the exact production path rather than an approximation of it. On
    `dev` — where the credential env vars are deliberately empty — fall back
    to a placeholder: the gateway substitutes the real value either way, so
    the calls still exercise the true end-to-end path.
    """
    try:
        from src.api.deps import get_config

        keys = get_config().api_keys
        return keys.openrouter, keys.alpaca_key, keys.alpaca_secret, keys.fred
    except Exception:
        return (PREFLIGHT_PLACEHOLDER,) * 4


def check_preflight(ctx: Ctx) -> None:
    """End-to-end provider preflight through QAMC's OWN client code.

    The `providers` group proves the gateway *injects* a credential at the
    HTTP level. That is necessary but not sufficient: it would still pass
    with a misspelled or retired model id, a token budget that yields no
    content, a market-data host QAMC can reach but not parse, or a FRED
    series that no longer resolves. This group answers the question the
    operator actually has — "will the pipeline work when commissioned?" —
    by constructing the same `openai`, `alpaca-py` and `fredapi` clients
    the trading engine builds and completing one real read with each.

    Opt-in (`--live`) because it makes authenticated calls with the real
    credentials and spends a trivial amount on one LLM completion. Every
    call here is read-only; no order is ever submitted.
    """
    group = "preflight"
    if not ctx.allow_network:
        ctx.add(group, "live provider preflight", SKIP, "--no-network")
        return
    if not ctx.live:
        ctx.add(group, "live provider preflight", SKIP,
                "not requested — pass --live to complete one real read per provider")
        return
    if not ctx.proxy:
        ctx.add(group, "live provider preflight", SKIP, "no gateway wiring resolved")
        return

    sys.path.insert(0, str(PROJECT_ROOT))
    openrouter_key, alpaca_key, alpaca_secret, fred_key = _credentials_for_preflight()

    # The SDKs read proxy/CA settings from the environment at request time,
    # which is exactly how the commissioned runtime supplies them. Set them
    # for this process so a pre-wiring run from `dev` exercises the same
    # path; a run from the wired runtime just re-affirms what is already set.
    os.environ["HTTPS_PROXY"] = ctx.proxy
    os.environ["https_proxy"] = ctx.proxy
    if ctx.ca_bundle:
        os.environ["SSL_CERT_FILE"] = ctx.ca_bundle
        os.environ["REQUESTS_CA_BUNDLE"] = ctx.ca_bundle

    _preflight_openrouter(ctx, group, openrouter_key)
    _preflight_alpaca(ctx, group, alpaca_key, alpaca_secret)
    _preflight_fred(ctx, group, fred_key)


def _preflight_openrouter(ctx: Ctx, group: str, api_key: str) -> None:
    # 1. Is the configured model id real and currently offered? A typo or a
    #    retired id passes every credential-level check and then fails on
    #    the first agent call of the first live session.
    try:
        import httpx

        with httpx.Client(timeout=30.0, proxy=ctx.proxy,
                          verify=ctx.ca_bundle or True, trust_env=False) as client:
            resp = client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            catalog = resp.json().get("data", []) if resp.status_code == 200 else []
    except Exception as exc:
        ctx.add(group, "openrouter model is available", SKIP,
                f"model catalog unreadable: {type(exc).__name__}: {exc}")
    else:
        ids = {m.get("id") for m in catalog}
        policy_models = sorted(set(EXPECTED_ROUTING.values()))
        if not ids:
            ctx.add(group, "openrouter models are available", SKIP,
                    "model catalog came back empty")
        else:
            absent = [m for m in policy_models if m not in ids]
            if absent:
                ctx.add(group, "openrouter models are available", FAIL,
                        f"{', '.join(absent)} NOT in OpenRouter's catalog of "
                        f"{len(ids)} models — the agents on those seats would "
                        "fail on their first call")
            else:
                ctx.add(group, "openrouter models are available", PASS,
                        f"all {len(policy_models)} policy models are offered "
                        f"({len(ids)} models in catalog)")

    # 2. A real completion per DISTINCT policy model, through the same client
    #    `src/agents/base.py` builds. Every model in the policy is exercised,
    #    not just one: "the credential chain works" and "this specific model
    #    answers" are different claims, and a seat whose model has been
    #    retired or gated fails on its first live session otherwise.
    #
    #    `max_tokens` is generous on purpose: reasoning models spend budget
    #    before emitting content, and a starved budget returns
    #    finish_reason='length' with nothing usable — which the agent layer
    #    correctly treats as an empty-response error and retries into.
    try:
        from openai import OpenAI

        from src.agents.base import _OPENROUTER_BASE_URL

        client = OpenAI(api_key=api_key, base_url=_OPENROUTER_BASE_URL,
                        timeout=120.0, max_retries=0)
    except Exception as exc:
        ctx.add(group, "openrouter completes a call", FAIL,
                f"client construction failed: {type(exc).__name__}: {str(exc)[:240]}")
        return

    for model in sorted(set(EXPECTED_ROUTING.values())):
        label = f"openrouter completes a call ({model})"
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=512,
            )
        except Exception as exc:
            ctx.add(group, label, FAIL, f"{type(exc).__name__}: {str(exc)[:300]}")
            continue

        choice = completion.choices[0] if completion.choices else None
        content = (getattr(getattr(choice, "message", None), "content", "") or "").strip()
        finish = getattr(choice, "finish_reason", None)
        if not content:
            ctx.add(group, label, FAIL,
                    f"model answered with no content (finish_reason={finish!r}) — "
                    "every agent call would raise LLMEmptyResponseError and burn "
                    "its retry budget")
            continue
        # `completion.model` is what OpenRouter says actually served the
        # request. A mismatch means the id was aliased to something else,
        # which breaks the attribution contract in
        # docs/architecture/MODEL_PROVIDER_ARCHITECTURE.md.
        served = getattr(completion, "model", "") or ""
        if served and served != model:
            ctx.add(group, label, WARN,
                    f"requested {model!r} but OpenRouter served {served!r} — "
                    "agent_logs would attribute output to a model that did not "
                    "produce it")
        else:
            ctx.add(group, label, PASS,
                    f"served by {served or model!r}, finish_reason={finish!r}, "
                    f"{len(content)} chars returned")


def _preflight_alpaca(ctx: Ctx, group: str, key: str, secret: str) -> None:
    try:
        from src.execution.broker import AlpacaBroker

        broker = AlpacaBroker(api_key=key, secret_key=secret, paper=True)
    except Exception as exc:
        ctx.add(group, "alpaca client constructs", FAIL,
                f"{type(exc).__name__}: {str(exc)[:300]}")
        return

    # The strongest available paper-only evidence: not what config claims,
    # but which host the SDK will actually talk to.
    endpoint = str(getattr(broker.client, "_base_url", "")) or "unknown"
    ctx.add(group, "alpaca endpoint is paper",
            PASS if "PAPER" in endpoint.upper() else FAIL,
            f"SDK resolved endpoint: {endpoint}")

    try:
        account = broker.get_account()
    except Exception as exc:
        ctx.add(group, "alpaca account reachable", FAIL,
                f"get_account failed: {type(exc).__name__}: {str(exc)[:300]}")
    else:
        # Shape only — a verification tool has no business printing balances.
        expected = {"cash", "portfolio_value", "last_equity"}
        missing = expected - set(account)
        numeric = all(isinstance(v, float) for v in account.values())
        ctx.add(group, "alpaca account reachable",
                PASS if not missing and numeric else FAIL,
                "account snapshot returned with all expected numeric fields"
                if not missing and numeric
                else f"missing={sorted(missing)} all_numeric={numeric}")

    try:
        bars = broker.get_bars("SPY", lookback_days=10)
    except Exception as exc:
        ctx.add(group, "alpaca market data", FAIL, f"{type(exc).__name__}: {exc}")
    else:
        # get_bars swallows its own errors and returns [], so an empty result
        # is the failure signal here — not an exception.
        ctx.add(group, "alpaca market data",
                PASS if bars else FAIL,
                f"{len(bars)} daily bars for SPY from data.alpaca.markets" if bars
                else "no bars returned — the market-data host is not usable "
                     "(check the *.alpaca.markets host scope in OneCLI)")

    try:
        price = broker.get_latest_price("SPY")
    except Exception as exc:
        ctx.add(group, "alpaca live quote", FAIL, f"{type(exc).__name__}: {exc}")
    else:
        ctx.add(group, "alpaca live quote",
                PASS if price and price > 0 else FAIL,
                "a positive price was returned" if price and price > 0
                else f"no usable price ({price!r}) — order sizing depends on this")

    try:
        # Consulted on every session entry; a failure here reads as "market
        # closed" and silently skips the whole session.
        broker.is_trading_day()
    except Exception as exc:
        ctx.add(group, "alpaca trading calendar", FAIL, f"{type(exc).__name__}: {exc}")
    else:
        ctx.add(group, "alpaca trading calendar", PASS,
                "calendar lookup succeeded (a failure here reads as "
                "market-closed and skips the session)")


def _preflight_fred(ctx: Ctx, group: str, api_key: str) -> None:
    try:
        from src.data.macro import MacroDataProvider

        vix = MacroDataProvider(api_key=api_key).get_vix()
    except Exception as exc:
        ctx.add(group, "fred series fetch", FAIL,
                f"{type(exc).__name__}: {str(exc)[:300]}")
        return

    # `_safe_get_series` degrades to an empty series on failure, which
    # surfaces as `current: None` — so None is the failure signal, not a raise.
    current = vix.get("current")
    if isinstance(current, float):
        ctx.add(group, "fred series fetch", PASS,
                f"VIXCLS returned an observation, staleness_days="
                f"{vix.get('staleness_days')}")
    else:
        ctx.add(group, "fred series fetch", FAIL,
                "VIXCLS returned no observation — macro analysis would run "
                "blind with regime detection seeing None freshness")


RUNTIME_ACCOUNT = "qamc"
DEV_ACCOUNT = "dev"
RUNTIME_HOME = Path("/home/qamc")


def check_isolation(ctx: Ctx) -> None:
    """The account boundary the credential architecture rests on.

    Two checklist items in `docs/WORK.md` live here: "`dev` cannot read the
    real QAMC credentials", and the isolation premise behind never adding
    `qamc`/`dev` to the `docker` group (which is root-equivalent on this
    host and would collapse the separation those accounts exist to create).
    """
    group = "isolation"
    import getpass

    try:
        account = getpass.getuser()
    except Exception as exc:
        ctx.add(group, "identify account", SKIP, f"{type(exc).__name__}: {exc}")
        return
    ctx.add(group, "running account", PASS, f"this check is running as {account!r}")

    if account == RUNTIME_ACCOUNT:
        ctx.add(group, "runtime credentials are unreadable off-account", SKIP,
                f"running AS {RUNTIME_ACCOUNT} — read access to its own home is "
                "expected; run from `dev` to check the boundary",
                resolved_by=DEV_ACCOUNT)
    else:
        try:
            list(RUNTIME_HOME.iterdir())
        except PermissionError:
            ctx.add(group, "runtime credentials are unreadable off-account", PASS,
                    f"{RUNTIME_HOME} is not readable from {account!r}")
        except FileNotFoundError:
            ctx.add(group, "runtime credentials are unreadable off-account", SKIP,
                    f"{RUNTIME_HOME} does not exist on this host")
        except Exception as exc:
            ctx.add(group, "runtime credentials are unreadable off-account", SKIP,
                    f"{type(exc).__name__}: {exc}")
        else:
            ctx.add(group, "runtime credentials are unreadable off-account", FAIL,
                    f"{account!r} can list {RUNTIME_HOME} — the account boundary "
                    "that keeps real credentials away from the dev workspace is "
                    "not holding")

    # Docker-group membership is root-equivalent on this host.
    try:
        proc = subprocess.run(["id", "-nG"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        ctx.add(group, "account is not in the docker group", SKIP, f"`id` failed: {exc}")
    else:
        groups = proc.stdout.split()
        in_docker = "docker" in groups
        ctx.add(
            group, "account is not in the docker group",
            FAIL if in_docker else PASS,
            f"{account!r} is in the docker group — that is root-equivalent and "
            "collapses the isolation this account exists to provide" if in_docker
            else f"{account!r} has no docker-socket access",
        )


def check_safety(ctx: Ctx) -> None:
    """Repo-side safety invariants that hold regardless of account."""
    group = "safety"

    # Trading timers must remain disabled until commissioning is accepted.
    # Only the runtime account can see its own user units. Critically, an
    # account that has NO quant-agent units at all (e.g. `dev`) must report
    # SKIP, not PASS — "I looked in the wrong place and found nothing" is
    # not evidence that the runtime's timers are off.
    def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["systemctl", "--user", "--no-pager", "--no-legend", *args],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    units = _systemctl("list-unit-files", "quant-agent*")
    if units is None or units.returncode != 0:
        ctx.add(group, "trading timers disabled", SKIP,
                "no access to a systemd --user session (run as the runtime account)",
                resolved_by=RUNTIME_ACCOUNT)
    elif not units.stdout.strip():
        ctx.add(group, "trading timers disabled", SKIP,
                "this account has no quant-agent systemd units — run as the "
                "runtime account to check timer state",
                resolved_by=RUNTIME_ACCOUNT)
    else:
        timers = _systemctl("list-timers", "--all", "quant-agent*")
        enabled = enabled_timer_units(units.stdout)
        active = [
            ln for ln in ((timers.stdout if timers else "") or "").splitlines()
            if "quant-agent" in ln and " n/a " not in ln
        ]
        problems = []
        if enabled:
            problems.append(
                f"{len(enabled)} timer unit(s) enabled: {', '.join(sorted(enabled))}"
            )
        if active:
            problems.append(f"{len(active)} timer(s) scheduled")
        ctx.add(
            group, "trading timers disabled",
            FAIL if problems else PASS,
            "; ".join(problems) + " — trading activation is a separate explicit "
            "decision" if problems
            else "quant-agent timer units present but none enabled/scheduled",
        )

    # No credential material committed to the repository.
    #
    # `tests/` is excluded deliberately. Key-SHAPED strings are legitimate
    # and necessary there — a test proving "this value is not a recognizable
    # placeholder" has to use something that looks like a real key, and the
    # repo already carries such a fixture in tests/test_base_agent.py. Left
    # in scope, this check would be permanently red on synthetic data, which
    # trains a reader to ignore it — strictly worse than a narrower check
    # that stays meaningful everywhere a real leak would actually land.
    try:
        proc = subprocess.run(
            ["git", "grep", "-nIE",
             r"(sk-or-v1-[A-Za-z0-9]{16,}|PK[A-Z0-9]{16,})",
             "--", ":(exclude)tests/"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        ctx.add(group, "no secrets committed", SKIP, f"git unavailable: {exc}")
    else:
        # `git grep` exits 1 when there are no matches — that is the pass.
        hits = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        ctx.add(
            group, "no secrets committed",
            FAIL if hits else PASS,
            f"{len(hits)} match(es) in tracked files — review immediately" if hits
            else "no live-credential patterns in tracked files (tests/ excluded — "
                 "synthetic key-shaped fixtures live there by design)",
        )

    # Mission Control must never gain a write route.
    routes_live = PROJECT_ROOT / "src" / "api" / "routes_live.py"
    try:
        text = routes_live.read_text()
    except OSError as exc:
        ctx.add(group, "mission control is read-only", SKIP, f"{routes_live}: {exc}")
    else:
        writes = [v for v in ("router.post", "router.put", "router.patch", "router.delete")
                  if v in text]
        ctx.add(
            group, "mission control is read-only",
            FAIL if writes else PASS,
            f"write routes present: {', '.join(writes)}" if writes
            else "GET-only router",
        )


# --------------------------------------------------------------------------
# Wiring resolution + entry point
# --------------------------------------------------------------------------


def resolve_wiring(ctx: Ctx, from_onecli: bool) -> None:
    """Populate ctx.proxy / ctx.ca_bundle.

    Preference order matters. The environment is checked first because that
    is what the *real* commissioned runtime looks like — verifying the
    actual wiring beats verifying a freshly-fetched copy of what the wiring
    should have been. `--from-onecli` is the pre-wiring fallback, so the
    provider proofs can be run from `dev` before the operator-only `.env`
    step has happened.
    """
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if env_proxy:
        ctx.proxy = env_proxy
        ctx.ca_bundle = (
            os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or None
        )
        ctx.proxy_source = "environment"
        return

    if not from_onecli:
        return

    try:
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://{LOOPBACK}:{ONECLI_DASHBOARD_PORT}/api/container-config", timeout=15,
        ) as resp:
            cfg = json.loads(resp.read().decode())
    except Exception as exc:
        ctx.add("wiring", "fetch config from onecli", FAIL,
                f"{type(exc).__name__}: {exc}")
        return

    # Shape (verified against the live instance): {"env": {...}, "caCertificate": "..."}.
    # OneCLI reports the proxy host as `host.docker.internal`; rewrite to
    # loopback, which is where the gateway is actually bound for bare
    # processes. check_wiring() still asserts the corrected form.
    env = cfg.get("env") or {}
    proxy = (
        env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    ).replace("host.docker.internal", LOOPBACK)
    cert = cfg.get("caCertificate") or ""
    if not proxy or not cert:
        ctx.add("wiring", "fetch config from onecli", FAIL,
                "container-config response lacked a proxy URL or CA certificate")
        return

    # Write the CA to a private temp file for this run only. Mode 0600 and
    # unlinked on exit — the CA is not secret, but leaving files behind in a
    # verification tool is how stale trust anchors happen.
    import atexit
    import tempfile

    fd, path = tempfile.mkstemp(prefix="qamc-onecli-ca-", suffix=".pem")
    with os.fdopen(fd, "w") as fh:
        fh.write(cert)
    os.chmod(path, 0o600)
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))

    ctx.proxy = proxy
    ctx.ca_bundle = path
    ctx.proxy_source = "onecli container-config"
    ctx.add("wiring", "fetch config from onecli", PASS,
            "resolved gateway wiring from the live instance")


GROUPS: dict[str, Callable[[Ctx], None]] = {
    "config": check_config,
    "gateway": check_gateway,
    "wiring": check_wiring,
    "providers": check_providers,
    "mission-control": check_mission_control,
    "preflight": check_preflight,
    "isolation": check_isolation,
    "safety": check_safety,
}

_ICON = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP", WARN: "WARN"}


def coverage_note(results: list[Result], account: str) -> str:
    """Say plainly whether this run was complete, and what completes it.

    Full acceptance spans two accounts by design: the runtime account owns
    the checks that need real credentials, its own systemd session and its
    environment, while the isolation check is only meaningful from OFF the
    runtime account — run there, it proves nothing. Without this note an
    operator reads a green single-account summary as full coverage, which
    is exactly the misreading the split invites.
    """
    pending: dict[str, list[str]] = {}
    for r in results:
        if r.status == SKIP and r.resolved_by and r.resolved_by != account:
            pending.setdefault(r.resolved_by, []).append(f"{r.group}/{r.name}")
    if not pending:
        return (
            f"ACCOUNT COVERAGE: complete from {account!r} — no check is waiting "
            "on another account."
        )
    out = [f"ACCOUNT COVERAGE: partial. Run from {account!r}; "
           f"{sum(len(v) for v in pending.values())} check(s) need another account:"]
    for who in sorted(pending):
        out.append(f"  as {who}: {', '.join(sorted(pending[who]))}")
    out.append("Acceptance is the union of both runs — see ops/onecli/README.md step 4e.")
    return "\n".join(out)


def render(results: list[Result], account: str = "?") -> str:
    lines: list[str] = []
    current = None
    for r in results:
        if r.group != current:
            current = r.group
            lines.append(f"\n[{current}]")
        line = f"  {_ICON[r.status]:<4}  {r.name}"
        if r.detail:
            line += f"\n          {r.detail}"
        lines.append(line)
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP, WARN)}
    lines.append(
        f"\n{counts[PASS]} passed, {counts[FAIL]} failed, "
        f"{counts[WARN]} warned, {counts[SKIP]} skipped"
    )
    if counts[FAIL] == 0 and counts[SKIP] == 0 and counts[WARN] == 0:
        lines.append("COMMISSIONING ACCEPTANCE: PASS")
    elif counts[FAIL] == 0:
        lines.append(
            "COMMISSIONING ACCEPTANCE: PASS (with skipped/warned checks — "
            "review them before accepting)"
        )
    else:
        lines.append("COMMISSIONING ACCEPTANCE: FAIL")
    lines.append("")
    lines.append(coverage_note(results, account))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify QAMC commissioning acceptance criteria (read-only).",
    )
    parser.add_argument(
        "--group", action="append", choices=sorted(GROUPS),
        help="run only this group (repeatable); default runs all",
    )
    parser.add_argument(
        "--from-onecli", action="store_true",
        help="resolve gateway wiring from the live OneCLI instance when the "
             "environment has none (pre-wiring verification)",
    )
    parser.add_argument("--no-network", action="store_true",
                        help="skip every outbound check")
    parser.add_argument(
        "--live", action="store_true",
        help="also complete one real read per provider through QAMC's own "
             "clients (authenticated; costs a trivial amount for one LLM call)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    ctx = Ctx(allow_network=not args.no_network, live=args.live)
    selected = args.group or list(GROUPS)

    if not args.no_network and ({"wiring", "providers", "preflight"} & set(selected)):
        resolve_wiring(ctx, args.from_onecli)

    for name in GROUPS:
        if name in selected:
            GROUPS[name](ctx)

    import getpass
    try:
        account = getpass.getuser()
    except Exception:
        account = "?"

    if args.json:
        print(json.dumps(
            {"account": account,
             "results": [r.__dict__ for r in ctx.results],
             "failed": sum(1 for r in ctx.results if r.status == FAIL),
             "pending_accounts": sorted({
                 r.resolved_by for r in ctx.results
                 if r.status == SKIP and r.resolved_by and r.resolved_by != account
             })},
            indent=2,
        ))
    else:
        print(render(ctx.results, account))

    return 1 if any(r.status == FAIL for r in ctx.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
