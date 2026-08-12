#!/usr/bin/env bash
# VPS baseline hardening — see ops/security/vps-hardening-plan.md for the findings
# and rationale this responds to. Run as ubuntu (needs root via sudo). Idempotent:
# safe to re-run: every section checks current state before changing anything.
#
# Deliberately does NOT: install Docker, touch qamc/dev sudo membership, touch
# ubuntu's access/sudoers/SSH keys, restrict SSH source IPs, change
# PasswordAuthentication/PermitRootLogin/any sshd_config value, enable Tailscale
# SSH, advertise routes or exit nodes, touch anything under QAMC's own runtime,
# or reboot the machine.

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (sudo bash ops/security/harden.sh ...)." >&2
  exit 1
fi

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

section() {
  echo
  echo "=== $1 ==="
}

# ---------------------------------------------------------------------------
section "fail2ban: install + sshd jail"
# ---------------------------------------------------------------------------
if dpkg -s fail2ban >/dev/null 2>&1; then
  echo "fail2ban already installed."
else
  run apt-get update
  run apt-get install -y fail2ban
fi

JAIL_LOCAL=/etc/fail2ban/jail.d/qamc-sshd.local
DESIRED_JAIL_CONTENT='[sshd]
enabled = true
port = ssh
backend = systemd
maxretry = 6
findtime = 10m
bantime = 1h
bantime.increment = true
'

if [ -f "$JAIL_LOCAL" ] && [ "$(cat "$JAIL_LOCAL")" = "$DESIRED_JAIL_CONTENT" ]; then
  echo "$JAIL_LOCAL already up to date."
else
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would write $JAIL_LOCAL:"
    echo "$DESIRED_JAIL_CONTENT" | sed 's/^/    /'
  else
    printf '%s' "$DESIRED_JAIL_CONTENT" > "$JAIL_LOCAL"
    echo "Wrote $JAIL_LOCAL"
  fi
fi

run systemctl enable --now fail2ban
if [ "$DRY_RUN" -eq 0 ]; then
  systemctl reload fail2ban 2>/dev/null || systemctl restart fail2ban
fi

# ---------------------------------------------------------------------------
section "ufw: baseline (deny-incoming by default, SSH + Tailscale explicitly allowed)"
# ---------------------------------------------------------------------------
if ! command -v ufw >/dev/null 2>&1; then
  run apt-get update
  run apt-get install -y ufw
fi

# Order matters: the allow rules must exist before default-deny + enable, so an
# already-idempotent re-run is also safe — ufw itself no-ops on duplicate rules.
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow OpenSSH
run ufw allow in on tailscale0
run ufw --force enable

if [ "$DRY_RUN" -eq 0 ]; then
  ufw status verbose
fi

# ---------------------------------------------------------------------------
section "pending package upgrades (version bumps only, no new/removed packages)"
# ---------------------------------------------------------------------------
run apt-get update
run apt-get upgrade -y

if [ "$DRY_RUN" -eq 0 ]; then
  if [ -f /var/run/reboot-required ]; then
    echo
    echo "REBOOT REQUIRED (kernel or core library update is staged)."
    echo "This script does not reboot. Run 'sudo reboot' manually when convenient."
    if [ -f /var/run/reboot-required.pkgs ]; then
      echo "Packages needing the reboot:"
      sed 's/^/  /' /var/run/reboot-required.pkgs
    fi
  else
    echo "No reboot required after this run."
  fi
fi

# ---------------------------------------------------------------------------
# Optional, NOT run by default even without --dry-run: widen unattended-upgrades
# to also auto-apply the ${distro_codename}-updates pocket, not just -security.
# Call explicitly: sudo bash ops/security/harden.sh --widen-auto-updates
# ---------------------------------------------------------------------------
widen_auto_security_updates() {
  section "unattended-upgrades: widen Allowed-Origins to include -updates pocket"
  local conf=/etc/apt/apt.conf.d/50unattended-upgrades
  local marker='"${distro_id}:${distro_codename}-updates";'
  if grep -qF "$marker" "$conf" && ! grep -qF "//$marker" "$conf"; then
    echo "-updates pocket already enabled in $conf."
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would uncomment the -updates line in $conf"
  else
    sed -i 's|^//\s*"\${distro_id}:\${distro_codename}-updates";|        "${distro_id}:${distro_codename}-updates";|' "$conf"
    echo "Uncommented -updates pocket in $conf"
  fi
}

for arg in "$@"; do
  if [ "$arg" = "--widen-auto-updates" ]; then
    widen_auto_security_updates
  fi
done

echo
echo "Done. Nothing here touched Docker, qamc/dev sudo, ubuntu's access, SSH source-IP restrictions, sshd_config, Tailscale routing/SSH/exit-node config, or QAMC's own runtime."
