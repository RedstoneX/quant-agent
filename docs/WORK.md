# QAMC Current Work

Status: **VPS DEPLOYMENT COMPLETE — DEVELOPMENT ENVIRONMENT ESTABLISHED — NEXT WORK BY AUTHORIZATION**

## Goal

Maintain the accepted QAMC system with clear separation between runtime and development environments.

The VPS deployment/hardening tranche is complete. The accepted bundle was deployed to the OVH VPS, verified, pushed, and independently reviewed.

## Current environment model

### Runtime

- Account: `qamc`
- Purpose: QAMC runtime only
- Location: `/home/qamc`
- Existing runtime services remain isolated from development tooling.

### Development

- Account: `dev`
- Purpose: Claude Code, engineering work, and future projects.
- Workspace: `/home/dev/projects`
- Current project:
  - `/home/dev/projects/quant-agent`

Claude Code is installed at user scope and used on demand. No persistent agent daemon is authorized.

## Deployment target

- Provider: OVH VPS.
- Hostname: `vps-37b5f875.vps.ovh.us`.
- IPv4: `135.148.120.105`.
- OS: Ubuntu 24.04.

## Completed verification

- Full deployed test suite: **1558 passed, 0 failed**.
- Mission Control API/UI deployed under supervised user systemd service.
- Runtime remained read-only/non-critical.
- Trading timers installed but intentionally controlled separately from runtime verification.
- Secrets remained outside Git.
- GitHub branch handoff completed and independently reviewed.

## Hard boundaries

- Alpaca Paper only.
- No deterministic trading/risk redesign.
- No broker-write Mission Control operations.
- No secrets in Git.
- No dedicated dashboard visualization/UX polish until separately authorized.
- No unnecessary infrastructure expansion.

## Engineering authority

Claude Code owns implementation inside an accepted work contract.

ChatGPT performs independent architecture review, governance checks, and acceptance checkpoints.

Do not migrate runtime into development accounts or collapse the separation model.
