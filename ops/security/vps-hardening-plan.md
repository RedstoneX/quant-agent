# VPS hardening plan — from the 2026-08-12 audit

> **Rescue note (2026-08-29):** This plan document was rescued onto `main` from the abandoned branch `claude/vps-security-hardening-t8m3qz` solely to fix a dangling reference from `docs/FUTURE_SECURITY_OBSERVATORY.md`. The accompanying script this plan describes, `ops/security/harden.sh`, still lives only on that branch — it has **never been applied to the box** and is **deliberately not merged** here (adding new lockdown/security infrastructure during stabilization needs a real justification, not a docs fix as the vehicle). If you need the script, fetch it from `claude/vps-security-hardening-t8m3qz:ops/security/harden.sh`. The "How to run it" section below still references that script/branch as it originally did; treat it as historical, not as instructions to execute against the current box without separate review.
>
> **Finding 1 below is PARTLY STALE — verified against the live box on 2026-08-29.** The audit it rests on was taken 2026-08-12, when `ufw` was inactive and `fail2ban` was not installed. Both are now **active**. So the two mitigations this plan proposes for finding 1 are, in substance, already in place — whoever applied them did not update this document, because the document was not on `main` to update.
>
> What remains true as of 2026-08-29: `sshd` still listens on `0.0.0.0:22` and `[::]:22`, i.e. SSH is still reachable from the public internet rather than being restricted to Tailscale, and the box still absorbs ordinary opportunistic scanning — **923 failed password attempts from 60 distinct source IPs in the preceding 24 hours**, down from the audit's 10,926 from 117 IPs over 48 hours, consistent with `fail2ban` now banning repeat offenders. The plan's own deliberate non-goals still stand unaddressed and are still owner decisions: it does not restrict which source IPs may reach port 22, and it does not touch `PasswordAuthentication` or `PermitRootLogin`.
>
> Treat every other finding in this document as **unverified against current state** until someone re-checks it the same way.

Source: `/tmp/qamc-vps-audit.txt` (produced by `ubuntu` at 2026-08-12 00:18 UTC on `vps-37b5f875`, `/tmp/qamc-vps-audit.sh` not readable by `dev`). This plan does not repeat that audit — findings below are cited from it plus a small set of things `dev` could and did verify directly (marked "confirmed live").

**No system changes have been made.** This is a plan and a reviewable script only, per instruction. Everything here needs operator approval before running, and the script itself must run as `ubuntu`, not `dev` or `qamc` — every action here is host-level administration, consistent with the existing account model (`ubuntu` = administration/recovery).

## Findings and what's proposed for each

### 1. SSH is publicly exposed and receiving brute-force traffic — propose: fail2ban + baseline UFW

Audit evidence: `sshd` listens on `0.0.0.0:22` and `[::]:22` (not restricted to Tailscale or any allowlist); UFW is `inactive`; no `nftables`/`iptables` rule filters port 22 (the only iptables/nft rules present are Tailscale's own, which govern the `tailscale0` interface, not the public one). Over the audit's 48-hour `journalctl -u ssh` window: **10,926** `Failed password` lines and **6,168** `Invalid user` lines from **117** distinct source IPs — an ordinary opportunistic internet-wide scan pattern (`root`, `admin`, `test`, `ubuntu`, `oracle`, `solana`, `ftpuser`, ... as attempted usernames), not a targeted attack, but a plain unmitigated exposure. `fail2ban` and `podman`/`docker` are both "not installed."

Proposed (script, sections `install_fail2ban` and `enable_baseline_ufw`):
- Install `fail2ban`, enable its `sshd` jail (ships enabled by default in Ubuntu's package; the script writes an explicit `jail.local` for `[sshd]` with conservative `maxretry`/`bantime` so it's declarative rather than relying on the package default). This bans an IP after repeated failures — it does not touch valid credentials or any account, and self-expires.
- Enable `ufw` with: default deny incoming, default allow outgoing, explicit `allow OpenSSH` (port 22/tcp) added **before** enabling (so the rule exists before the default-deny takes effect — this is the standard safe ordering; getting this backwards is the classic way people lock themselves out), and an explicit `allow in on tailscale0` rule so all Tailscale-network traffic keeps working unconditionally.
- **What this deliberately does not do:** it does not restrict *which* source IPs can reach port 22 — SSH stays reachable from the public internet exactly as it is now, just with brute-force attempts throttled/banned and every other port default-denied. It does not touch `PasswordAuthentication`, `PermitRootLogin`, or any `sshd_config` value — the audit shows `ubuntu` actually using password auth from at least one recent session (`Accepted password for ubuntu from 174.88.8.166`), and changing that without confirming the operator's actual access method first risks exactly the lockout the hard boundaries prohibit. That's a separate, later decision if wanted, not part of this pass.

### 2. Tailscale is installed and connected but not a subnet router or exit node — no action proposed

Audit evidence: `tailscaled` active, connected as `rexredstone@gmail.com` (100.111.170.97), `tailscale debug prefs` shows `RouteAll: false`, empty `ExitNodeID`/`ExitNodeIP`. This is current state, not a defect — the hard boundaries explicitly say not to configure subnet routes, exit nodes, or Tailscale SSH in this pass. Noted here only because the audit called it out; no script action corresponds to it.

### 3. `qamc` and `dev` currently have `sudo` — no action proposed

Audit evidence: `getent group sudo` → `sudo:x:27:qamc,dev`; both resolve to `(ALL : ALL) ALL` (not `NOPASSWD`, unlike `ubuntu`'s `(ALL) NOPASSWD: ALL`). Per the explicit hard boundary this pass does not remove sudo from either account. Recorded here as a candidate for a **later, separate** hardening step (least-privilege — neither account should need standing sudo once the accounts' actual routine needs are fully enumerated) that needs its own approval, since removing sudo from `dev` would, for instance, affect how future privileged troubleshooting from `dev` works.

### 4. `ubuntu` remains the admin/recovery account — no action proposed

Confirms the intended model; the script preserves this by construction (it must be run as `ubuntu`, it never modifies `ubuntu`'s sudoers entry, SSH access, or authorized_keys).

### 5. Docker is not installed — no action here (tracked separately)

Audit evidence: "Docker not installed / Podman not installed" — confirmed live by `dev` independently in the OneCLI commissioning work (`command -v docker` → not found). Docker provisioning is already specified, reviewed, and pending separate approval in [ops/onecli/README.md](../onecli/README.md) as part of OneCLI commissioning, not this hardening pass. Excluded here per the explicit hard boundary.

### 6. `fake_provider.py` — identified; not part of this pass

Audit evidence: `PROCESS SNAPSHOT` shows `dev 78680 ... python3 fake_provider.py 9443`, still running. Checked directly (as `dev`, no privilege needed): PID 78680 is a leftover orphaned process from the disposable local test harness built during the now-reverted custom credential-proxy work (`docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`) — its working directory (`/tmp/claude-.../scratchpad/proxytest`) is already deleted, confirming it's an orphan from that prior, separate session. It listens on `127.0.0.1:9443` only (not `0.0.0.0`, no external exposure) and is owned by `dev`, not a system service.

This is not a security finding requiring `ubuntu`/root action — it's `dev`'s own stray process. Not included in the `ubuntu`-run script. If wanted, it can be cleaned up independently and trivially (`kill 78680`, or more robustly `pkill -f 'python3 fake_provider.py'` after confirming ownership) whenever approved — separate from, and much lower-risk than, everything else in this plan.

### 7. Pending kernel/security updates — propose: apply pending package upgrades (no auto-reboot)

Audit evidence: `apt list --upgradable` lists 28 packages, including the `systemd`/`libpam-systemd`/`libnss-systemd`/`udev`/`systemd-resolved` stack (several tagged `noble-security`) and others (`apparmor`, `cloud-init`, `fwupd`, ...) from `noble-updates`. Separately, `REBOOT REQUIRED` lists `linux-image-6.8.0-137-generic` — a newer kernel is already installed (`unattended-upgrades` pulled it in) but not yet booted into; the running kernel is still `6.8.0-106-generic` (confirmed live by `dev` via `uname -a`).

Proposed (script, section `apply_pending_upgrades`):
- `apt-get update && apt-get upgrade -y` — every listed package is a version bump of something already installed; nothing here adds or removes packages (`upgrade`, not `dist-upgrade`), so this shouldn't change what's running beyond patching it.
- Report reboot-required status clearly at the end. **The script does not reboot.** A reboot is disruptive enough (drops the current SSH session, momentarily takes down Mission Control/whatever else is running) that it belongs to a moment the operator explicitly chooses, not something bundled into an unattended script run. The plan recommends running `sudo reboot` manually once convenient, after this step.
- Optional, separately callable, off by default: `widen_auto_security_updates` — adds `${distro_id}:${distro_codename}-updates` to `/etc/apt/apt.conf.d/50unattended-upgrades`'s `Allowed-Origins` so future non-security `-updates` (like the `apparmor`/`cloud-init`/`fwupd` packages seen here) get picked up automatically too, not just the `-security` pocket. This is a judgment call (broader auto-updates = less manual toil but a very marginally larger blast radius if an update ever regresses something) so it's not run by default even when the rest of the script is approved — call it explicitly if wanted.

## What this plan explicitly does not touch (hard boundaries, restated)

- Does not install Docker.
- Does not remove `sudo` from `qamc` or `dev`.
- Does not modify `ubuntu`'s access, sudoers entry, or SSH keys — `ubuntu` recovery access is preserved by construction.
- Does not restrict which IPs can reach SSH, and does not change `PasswordAuthentication`/`PermitRootLogin`/any `sshd_config` value — current SSH access is preserved exactly as-is.
- Does not enable Tailscale SSH, advertise routes, or configure an exit node.
- Does not touch anything under `/home/qamc/quant-agent`, QAMC's systemd units, `config/settings.yaml`, or any trading/risk/Mission Control code or config.
- Does not reboot the VPS.

## How to run it (once approved)

```bash
# as ubuntu, on the VPS
git -C /tmp/qamc-ops-checkout pull 2>/dev/null || git clone git@github.com-quant-agent:RedstoneX/quant-agent.git /tmp/qamc-ops-checkout
cd /tmp/qamc-ops-checkout && git checkout claude/vps-security-hardening-t8m3qz
sudo bash ops/security/harden.sh --dry-run   # review exactly what would change, changes nothing
sudo bash ops/security/harden.sh             # applies fail2ban + UFW baseline + pending package upgrades
```

Each section is independently idempotent — safe to re-run the whole script later (e.g. after a fresh audit) without re-doing or duplicating anything already applied.
