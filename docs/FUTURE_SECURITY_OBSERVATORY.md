# Mission Control Security Observatory

**Status: CONCEPTUAL / NOT AUTHORIZED FOR IMPLEMENTATION.**

This document records an idea for a future Mission Control panel surfacing host/network security posture alongside trading state. It does not authorize building it, and it does not add Grafana, Prometheus, Loki, or any other monitoring service to the current architecture. Mission Control remains read-only and non-critical to trading; a future Security Observatory panel would be a read-only view of existing host state, not a new authority.

## Motivation

VPS baseline hardening (UFW, fail2ban, Tailscale — see `ops/security/vps-hardening-plan.md`) currently has no visibility inside QAMC's own operator surface; checking it means SSHing in and reading `journalctl`/`ufw status`/`fail2ban-client` by hand. A dedicated panel would let the operator see security posture from the same iPad/browser cockpit used for everything else.

## Potential panel contents

- SSH attack attempts (failed/invalid-user login rate, source-IP counts)
- fail2ban blocks (current bans, ban history)
- firewall events (UFW deny/allow activity)
- active connections
- exposed ports (what's actually listening, and on which interface)
- service/container health
- unusual resource or network activity

## Explicit non-goals for now

- No Grafana, Prometheus, Loki, or other dedicated monitoring/observability service. If this is ever built, it should first be evaluated as a small read-only data pull into Mission Control's existing API/UI, consistent with QAMC's "avoid unnecessary infrastructure" principle — not a new service stack, unless a real future evaluation concludes otherwise and gets separate approval.
- Not a control surface — this panel would only ever display state, never take action (no ban/unban, no firewall edits, no service restarts from the UI).
- Not part of the current commissioning or hardening work. Building this requires its own future authorization like any other dashboard capability, per `docs/OUTCOME.md`'s MVP lifecycle principle (visualization/UX work follows deployed-MVP acceptance, not before).
