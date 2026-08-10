# QAMC Current State

Updated: 2026-08-09

This file says what is accepted and authorized **now**. Git history preserves prior state and discovery evidence.

## Accepted

- Stages 0, 0.5, 1, 2, 3, 4 and 5 are accepted.
- Stage 2 delivered the isolated read-only Mission Control API and deterministic `risk_gate` forensic persistence without changing trading/risk semantics.
- Stage 3 delivered the read-only browser/iPad Trading Cockpit at `/ui`; accepted verification: **1531 passed, 0 failed** plus desktop/iPad runtime review.
- Discovery R1 and ChatGPT reconciliation are accepted.
- Stage 4 delivered per-candidate specialist evidence + decision-chain drill-down while preserving each specialist's real data scope and existing `decision_id` semantics.
- Stage 5 delivered read-only journal and parameterized forensic search. Stage 4–5 passed external ChatGPT/operator review with **1558 passed, 0 failed** plus committed browser/runtime evidence.
- PR #24 is merged into `main` as merge commit `105cc91a14faebd8a981061b3098eb181b306dda`.
- The permanent frontend-verification requirement under `.claude/rules/frontend-verification.md` remains accepted.
- Cloud/ephemeral development environments are staging only. The QAMC MVP is not operationally accepted until the integrated product is deployed to the intended VPS/server runtime, verified there, independently reviewed, and accepted through operator UAT.
- Dedicated Mission Control visualization/UX polish remains after that deployed-MVP gate.

## Authorized now

**VPS cutover / deployment hardening and deployed-runtime verification** are authorized as the next bounded engineering tranche. See `docs/WORK.md` for the exact contract.

Claude may investigate the repository and choose implementation details, subagents and safe parallelism inside that contract. The tranche ends with a pushed branch and checkpoint report for independent ChatGPT review; Claude does not merge its own work.

## Not authorized now

- deterministic trading/risk behavior changes;
- broker-write Mission Control controls;
- live trading;
- dedicated TradingView/donor-dashboard/visual-polish work;
- later learning/write-control stages;
- any product expansion unrelated to deployment, runtime hardening or the deployed-MVP verification gate.

## Handoff

**Operational correction (2026-08-10):** Anthropic's cloud environment could not open outbound SSH (raw TCP/22 blocked there). Claude Code now runs through a Mac-hosted SSH connection directly to the OVH VPS as user `qamc`. The disposable-cloud-bootstrap-key plan in the original handoff wording never happened; `authorized_keys` on the VPS was checked and already contained exactly one key (`qamc-vps-deploy-20260809`, the persistent Mac-hosted key) — no leftover bootstrap credential existed to revoke.

Progress this tranche (branch `claude/vps-deployment-hardening-q3f7k2`):
- Python env bootstrapped on the VPS without root (`python3 -m venv --without-pip` + `get-pip.py`; the `qamc` user has no working sudo in this session — `sudo -n` fails, no NOPASSWD rule). `pip install -e '.[api,dev]'` succeeded.
- Full test suite run in the deployed venv: **1558 passed, 0 failed** — matches the accepted baseline.
- Mission Control API/UI deployed as a supervised `systemd --user` service (`quant-agent-api.service`, `Restart=always`), bound to `127.0.0.1:8800` only (no public exposure; matches `.env.example`'s documented private-networking intent). Verified: `/health` and `/ui` return 200, `db_reachable: true`, graceful `broker_reachable: false` degradation with placeholder keys, kill -9 crash-recovery within `RestartSec=5`, survives logout via `loginctl enable-linger qamc` (already `Linger=yes`) + unit `enable`.
- systemd `--user` timer/service units installed for all six trading modes (`earnings_preprocess/morning/intra_check/midday/close/evening`, every-30-min self-gated per `scripts/run_if_et_window.sh`) plus the daily P&L export — installed via `daemon-reload` but deliberately left **disabled**, not started.

Two items remain genuinely blocked on operator action and were not worked around:
1. **No API secrets exist anywhere on the VPS.** `.env` is currently the unmodified `.env.example` template (placeholder values only, `chmod 600`). Real `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `FRED_API_KEY` must reach the VPS via a channel other than chat (e.g. `scp`/`sftp` straight to `/home/qamc/quant-agent/.env`, or edit the file directly over SSH) before the trading timers can be enabled or "exercise the deployed engine" verification can run for real.
2. **No root available** for `sudo apt-get install` of the shared libraries (`libatk-1.0.so.0` and others) headless Chromium needs for full visual/screenshot browser verification per `.claude/rules/frontend-verification.md`. HTTP-level runtime verification (`/health`, `/ui`, restart/crash recovery) is done; pixel-level desktop/iPad screenshot verification is not.

The tranche therefore stops here as a **bounded, honest checkpoint** — infra deployed and verified where root/secrets were not required; live-trading and full visual verification remain open. Operator UAT and MVP acceptance still happen only after independent review, and only once the two blockers above are cleared and re-verified. Dedicated visualization/UX polish is not authorized until the deployed MVP is accepted.
