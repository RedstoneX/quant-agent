#!/usr/bin/env bash
# QAMC Telegram trader-feed rollout launcher v2.
# Fixes the pre-mutation test-archive parent-directory traversal defect in v1.
set -Eeuo pipefail

DEV_REPO="/home/dev/projects/quant-agent"
SOURCE_BRANCH="ops/telegram-trader-feed-rollout"
SOURCE_BLOB="bf854acea2e253ae616155f78250e05822728a38"
SOURCE_SHA256="726fb62b4bbb97477ccd60c6fab865f36853f707e0c2583acca7cfbd7251a312"

TMPD="$(mktemp -d /tmp/qamc-telegram-v2.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root"
SELF="$(readlink -f "$0")"
[[ "$(stat -c '%U:%G:%a' "$SELF")" == "root:root:700" ]] \
  || die "launcher must be root:root mode 700"
[[ "$(stat -c '%U:%G:%a' /root)" == "root:root:700" ]] \
  || die "/root must be root:root mode 700"
[[ -d "$DEV_REPO/.git" ]] || die "dev repository missing"

sudo -u dev -H git -C "$DEV_REPO" fetch --no-tags origin \
  "+refs/heads/${SOURCE_BRANCH}:refs/remotes/origin/${SOURCE_BRANCH}" >/dev/null 2>&1 \
  || die "could not fetch rollout branch"

sudo -u dev -H git -C "$DEV_REPO" cat-file -e "${SOURCE_BLOB}^{blob}" \
  || die "reviewed v1 launcher blob unavailable"
sudo -u dev -H git -C "$DEV_REPO" cat-file blob "$SOURCE_BLOB" >"$TMPD/v1.sh"
echo "${SOURCE_SHA256}  $TMPD/v1.sh" | sha256sum -c - \
  || die "v1 launcher SHA256 mismatch"

python3 - "$TMPD/v1.sh" "$TMPD/v2.sh" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
old = b'TMPD="$(mktemp -d /tmp/qamc-telegram-launcher.XXXXXX)"\ntrap \'rm -rf "$TMPD"\' EXIT\n'
new = b'TMPD="$(mktemp -d /tmp/qamc-telegram-launcher.XXXXXX)"\nchmod 0711 "$TMPD"\ntrap \'rm -rf "$TMPD"\' EXIT\n'
count = data.count(old)
if count != 1:
    raise SystemExit("expected exactly one tmpdir block, found %d" % count)
open(dst, "wb").write(data.replace(old, new))
PY

install -o root -g root -m 0700 "$TMPD/v2.sh" /root/qamc-telegram-trader-feed-launcher-v2-derived.sh
exec /root/qamc-telegram-trader-feed-launcher-v2-derived.sh
