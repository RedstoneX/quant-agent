# QAMC Current Work

Status: **POST-MERGE RUNTIME ACCEPTANCE**

Accepted implementation: PR #30 merged to `main` as `7b78f72ecfc900e166af2207a6f2a8473c277131`.

## Goal

Close the one remaining privileged runtime acceptance step against accepted `main`. Do not reopen model-routing or decision-chain architecture unless the runtime evidence exposes a real defect.

## What is already closed

- Cost-optimized OpenRouter routing is accepted: eight seats on `google/gemini-2.5-flash-lite`, `risk_manager` on `qwen/qwen3-235b-a22b-2507`.
- The RM split was re-measured on the current prompt/input path: four candidates tied at 1.00 mean / 1.00 worst; independence then latency/cost selected Qwen for RM.
- The price-as-quality proxy is removed and replaced with seat-specific benchmark evidence for decision seats.
- OpenRouter `vendor/model` pricing no longer falls through to LiteLLM direct-provider rates.
- The deferred agent-audit findings F4/F5/F6/F7b/F8 are accepted.
- Full suite at the reviewed implementation head: **1829 passed, 0 skipped**.
- The `dev` commissioning half already passed with live OpenRouter preflight for both policy models and proved the off-account isolation boundary.
- `verify_pricing.py` passed for all pinned OpenRouter rates.
- Alpaca remains Paper-only; Mission Control remains read-only; trading timers remain disabled.
- No deterministic risk or execution semantics changed.

Architecture/evidence:
- `docs/architecture/MODEL_ROUTING_POLICY.md`
- `docs/architecture/DECISION_CHAIN_AUDIT.md`

## Remaining operator-only step

The `qamc` runtime account is now synchronized to accepted `main`. The first runtime acceptance attempt on 2026-08-14 exposed three **invocation/environment issues**, not architecture defects:

1. `python3` used the system interpreter, which does not carry QAMC's runtime dependencies (`pydantic` missing). Use `.venv/bin/python`.
2. A `sudo -u qamc -i` shell did not populate the `systemd --user` bus environment. Export `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` before `systemctl --user`.
3. `.env` is read by the service/trading launcher but is not automatically exported into an interactive verification shell. Export it before running the verifier so the runtime wiring check inspects the actual `HTTPS_PROXY`, `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` values.

From the existing `qamc` shell:

```bash
cd /home/qamc/quant-agent

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

set -a
. ./.env
set +a

systemctl --user daemon-reload
systemctl --user restart quant-agent-api.service
systemctl --user status quant-agent-api.service --no-pager

.venv/bin/python ops/commissioning/verify_commissioning.py --live
```

The `qamc` run must exit `0` with **zero FAIL results**. It will still report the off-account isolation check as `SKIP` / `ACCOUNT COVERAGE: partial`, because that check is intentionally only meaningful from `dev`. That is expected. **Full commissioning acceptance is the union of the already-green `dev` run and this green `qamc` run; no single-account run can report complete cross-account coverage by itself.**

Do not use `--from-onecli` on the `qamc` run: runtime acceptance must verify the environment actually used by QAMC, not a temporary wiring copy fetched from OneCLI.

If `systemctl --user` still cannot reach the bus after the two exports above, stop and report that exact error; do not redesign architecture.

If the verifier fails after this corrected invocation, capture the failing check names and output only.

## After runtime acceptance

1. Record the accepted runtime evidence in `docs/STATE.md` / this file.
2. Keep trading timers disabled until the operator explicitly authorizes the Alpaca Paper soak.
3. Once authorized, timer activation is routine deployment, not a new architecture decision.

## Hard boundaries

- Alpaca **Paper only**; no live trading.
- Preserve Specialist Agents → Portfolio Manager → AI Risk Manager → deterministic Python risk/execution.
- No deterministic risk/execution semantic redesign.
- No broker-write Mission Control controls.
- No public services.
- No collapse of `dev` / `qamc` / `ubuntu` isolation.
- No replacement for upstream OneCLI.
- No new durable routing infrastructure.
- No secrets in Git/chat/logs/screenshots/client evidence.
- No silent model fallback or unrecorded model choice.
- Claude does not merge its own PR or push implementation directly to `main`.
