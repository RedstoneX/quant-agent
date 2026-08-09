# QAMC Current Work

Status: **VPS DEPLOYMENT / HARDENING AUTHORIZED — BUILD, VERIFY, PUSH, STOP**

## Goal

Move the accepted QAMC paper-trading engine + read-only Mission Control bundle from cloud/ephemeral staging into the intended small Linux VPS runtime, harden the operational deployment, verify the deployed system there, and produce a bounded branch/checkpoint for independent review.

This is a deployment tranche, not a redesign. Preserve the accepted trading engine, read-only Mission Control boundary and Stage 4–5 behavior unless a real deployment defect requires a narrowly-scoped fix.

## Deployment target

- Provider: OVH VPS.
- Hostname: `vps-37b5f875.vps.ovh.us`.
- IPv4: `135.148.120.105`.
- OS: Ubuntu 24.04.
- Storage: 100 GB; no additional storage purchased.
- Automated daily OVH backup: active.
- Current plan: $14.50/month, no commitment.
- Operator is currently working entirely from an iPad; do not require a desktop/laptop merely to bootstrap or operate this tranche.

## SSH bootstrap prerequisite

Claude Code is currently running in Anthropic's cloud environment. Use that environment for the initial disposable SSH bootstrap credential:

1. Generate a disposable Ed25519 keypair in the Claude cloud environment.
2. Keep the private key only in that environment. Never place it in chat, GitHub, repository files, logs intended for commit, or client/UI artifacts.
3. Give the operator only the public key, then stop for the operator to install it in OVH from the iPad.
4. After operator confirmation, verify SSH access before doing deployment work.
5. Establish the persistent VPS access/runtime arrangement appropriate to the accepted architecture.
6. Before checkpoint handoff, remove/revoke the disposable bootstrap credential once it is no longer needed and verify that revocation did not break the intended persistent access path.

Do not expose or commit secrets.

## Authorized engineering scope

Claude owns repository-level deployment planning and may choose the simplest safe implementation details after inspecting the actual codebase. The outcome must establish, as appropriate to this repository:

- reproducible deployment of the accepted `quant-agent` engine and Mission Control API/UI bundle to the VPS;
- Python/runtime/system dependencies needed on Ubuntu 24.04;
- secrets/environment configuration outside Git and client surfaces;
- durable application/data paths and permissions;
- process supervision and restart-on-failure/reboot behavior for the trading engine and Mission Control as separate operational components where required by the accepted non-critical read-side boundary;
- private operator access rather than unnecessary public exposure;
- logs, health checks and basic operational recovery sufficient to distinguish trading-engine failure from Mission Control failure;
- backup/recovery handling consistent with the existing OVH daily backup and the application's persisted artifacts;
- evidence that Mission Control failure still has zero effect on deterministic trading/risk/broker protections.

Claude may add deployment scripts/configuration/documentation where genuinely useful. Avoid unnecessary infrastructure, distributed services, containerization or reverse-proxy complexity unless the repository/runtime evidence justifies them.

## Verification and acceptance evidence

Before handoff, Claude must:

- run the full applicable automated test suite in the deployed/runtime context;
- exercise the deployed engine/API/UI paths and health behavior on the VPS;
- browser/runtime verify the cockpit using the permanent frontend-verification rule, including representative desktop and iPad-sized scenarios and honest populated/empty/error/degraded states relevant to this deployment;
- verify restart/reboot recovery and persistent data behavior;
- verify Mission Control can fail/restart independently without weakening or stopping the trading engine's deterministic protections;
- verify no secret material entered Git or committed evidence;
- remove/revoke the disposable SSH bootstrap credential when persistent access is established;
- update `STATE.md`, `WORK.md` and the human `PROJECT_COMPASS.md` only as needed for the checkpoint;
- commit and push the bounded deployment branch, then **STOP** with a concise checkpoint report for ChatGPT/operator review.

Operator UAT happens only after fresh independent review of the pushed result. Claude must not declare the deployed MVP accepted on its own.

## Hard boundaries

- Alpaca **Paper only**.
- No deterministic trading/risk semantic redesign.
- No broker-write Mission Control operations.
- No secrets or fake production trading state in client/UI surfaces or Git.
- Mission Control remains read-only and non-critical to trading.
- Do not start dedicated dashboard visualization/UX polish.
- Do not start later learning/write-control work.
- Claude does not merge PRs, force-push, or push implementation directly to `main`.

## Engineering authority and escalation

Within this contract Claude should act as engineering lead: inspect the repository, select implementation architecture, choose subagents/workers and maximize safe parallelism without asking the operator to make routine technical decisions.

Escalate only for a genuine operator product/value trade-off, a material conflict with accepted architecture/safety/scope, or an external-access decision that cannot be safely inferred. Otherwise implement, verify, integrate, push and stop at the checkpoint.
