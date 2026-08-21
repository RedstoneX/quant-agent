#!/usr/bin/env bash
# QAMC Telegram trader-feed rollout launcher.
#
# Purpose: deploy exact target e113f5c... from the currently accepted
# production baseline d14e28d... while preserving the authorized
# intraday_scan.enabled=true production-local override.
#
# This does not invent a second deployment system. It provenance-verifies the
# already-hardened recovery rollout blob that successfully deployed PR #56,
# applies exactly five pinned identity substitutions, runs Telegram-focused
# tests against an archive of the exact target BEFORE production mutation, then
# executes the resulting root-owned 0700 rollout.
#
set -Eeuo pipefail

BASELINE_SHA="d14e28dfc63ca6e4da920229b0ab5ba0f33b93df"
TARGET_SHA="e113f5c6255925f1a93f0f8c242dcd5facbaf41a"
TARGET_TREE="6631860fdc2e79b6f66c2edb9c14207c374bce5a"
EXPECTED_CHANGED_FILES="6"
EXPECTED_FILELIST_SHA="ca760a33ea8563cb2ccf55586c94d51cc084347c357dc70adc80c92593f563f4"

DEV_REPO="/home/dev/projects/quant-agent"
DEV_VENV="${DEV_REPO}/.venv/bin/python"
QAMC_REPO="/home/qamc/quant-agent"

SOURCE_BRANCH="claude/trading-utility-recovery-rollout"
SOURCE_BLOB="13661bdad8d6df83dd2cee048b6d6727f3e5c582"
SOURCE_SHA256="f77d7ee2512016a9fb0cf718a6dc4877c0e2077a771cf0ac2af80c7521417712"

PATCHED="/root/qamc-telegram-rollout.sh"
TMPD="$(mktemp -d /tmp/qamc-telegram-launcher.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

say() { printf '\n== %s ==\n' "$*"; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root"
SELF="$(readlink -f "$0")"
[[ "$(stat -c '%U:%G:%a' "$SELF")" == "root:root:700" ]] \
  || die "launcher must be root:root mode 700"
[[ "$(stat -c '%U:%G:%a' /root)" == "root:root:700" ]] \
  || die "/root must be root:root mode 700"

id dev >/dev/null 2>&1 || die "dev account missing"
id qamc >/dev/null 2>&1 || die "qamc account missing"
[[ -d "$DEV_REPO/.git" ]] || die "dev repository missing"
[[ -d "$QAMC_REPO/.git" ]] || die "production repository missing"
[[ -x "$DEV_VENV" ]] || die "dev virtualenv python missing"

say "Preflight exact production baseline"
HEAD_NOW="$(sudo -u qamc -H git -C "$QAMC_REPO" rev-parse HEAD)"
[[ "$HEAD_NOW" == "$BASELINE_SHA" ]] \
  || die "production HEAD is $HEAD_NOW, expected $BASELINE_SHA"

DIRTY="$(sudo -u qamc -H git -C "$QAMC_REPO" status --porcelain)"
[[ "$DIRTY" == " M config/settings.yaml" ]] \
  || die "production local delta is not exactly config/settings.yaml"

# Read-only verification of the accepted false -> true intraday override.
BASE_CFG="$TMPD/baseline-settings.yaml"
sudo -u qamc -H git -C "$QAMC_REPO" show \
  "$BASELINE_SHA:config/settings.yaml" >"$BASE_CFG"
python3 - "$BASE_CFG" "$QAMC_REPO/config/settings.yaml" <<'PY' \
  || die "production config is not exactly the authorized intraday override"
import re, sys
base = open(sys.argv[1]).read().splitlines()
work = open(sys.argv[2]).read().splitlines()
if len(base) != len(work):
    raise SystemExit("line count differs")
diffs = [(i, a, b) for i, (a, b) in enumerate(zip(base, work)) if a != b]
if len(diffs) != 1:
    raise SystemExit("expected one changed line, found %d" % len(diffs))
idx, a, b = diffs[0]
in_block = False
enabled_idx = None
for i, line in enumerate(base):
    if re.match(r"^intraday_scan:\s*$", line):
        in_block = True
        continue
    if in_block:
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        m = re.match(r"^(\s*)enabled:\s*(\S+)(.*)$", line)
        if m:
            if m.group(2) != "false":
                raise SystemExit("committed intraday enabled value is not false")
            enabled_idx = i
            expected = "%senabled: true%s" % (m.group(1), m.group(3))
            if idx != i or b != expected:
                raise SystemExit("working delta is not exact false -> true")
            break
if enabled_idx is None:
    raise SystemExit("intraday_scan.enabled not found")
PY

say "Fetch and verify exact Telegram target in dev"
sudo -u dev -H git -C "$DEV_REPO" fetch --no-tags origin \
  "+${TARGET_SHA}:refs/qamc/telegram-target" >/dev/null 2>&1 \
  || sudo -u dev -H git -C "$DEV_REPO" fetch --no-tags origin main >/dev/null 2>&1 \
  || die "could not fetch target"

ACTUAL_TREE="$(sudo -u dev -H git -C "$DEV_REPO" rev-parse "${TARGET_SHA}^{tree}")"
[[ "$ACTUAL_TREE" == "$TARGET_TREE" ]] \
  || die "target tree mismatch: $ACTUAL_TREE"

CHANGED="$(sudo -u dev -H git -C "$DEV_REPO" diff --name-only "$BASELINE_SHA" "$TARGET_SHA")"
CHANGED_COUNT="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
CHANGED_SHA="$(printf '%s\n' "$CHANGED" | sha256sum | cut -d' ' -f1)"
[[ "$CHANGED_COUNT" == "$EXPECTED_CHANGED_FILES" ]] \
  || die "target delta file count is $CHANGED_COUNT, expected $EXPECTED_CHANGED_FILES"
[[ "$CHANGED_SHA" == "$EXPECTED_FILELIST_SHA" ]] \
  || die "target changed-file list differs from reviewed six-file set"

sudo -u dev -H git -C "$DEV_REPO" grep -qF \
  'from src.trader_feed import format_session_result' "$TARGET_SHA" -- main.py \
  || die "main.py trader-feed wiring absent"
sudo -u dev -H git -C "$DEV_REPO" grep -qF \
  'from src.trader_feed import format_session_result' "$TARGET_SHA" -- src/scheduler.py \
  || die "scheduler trader-feed wiring absent"
sudo -u dev -H git -C "$DEV_REPO" grep -qF \
  'mode=ro' "$TARGET_SHA" -- src/trader_feed.py \
  || die "read-only SQLite contract absent"
sudo -u dev -H git -C "$DEV_REPO" grep -qF \
  'intraday_scan' "$TARGET_SHA" -- src/trader_feed.py \
  || die "intraday opportunity visibility path absent"

say "Run focused Telegram tests on exact target archive"
TESTDIR="$TMPD/target"
install -d -o dev -g dev -m 0700 "$TESTDIR"
sudo -u dev -H bash -c \
  "git -C '$DEV_REPO' archive '$TARGET_SHA' | tar -x -C '$TESTDIR'"
set +e
TEST_OUT="$(sudo -u dev -H bash -c \
  "cd '$TESTDIR' && '$DEV_VENV' -m pytest \
   tests/test_trader_feed.py tests/test_notifier.py tests/test_scheduler.py \
   -q -p no:randomly" 2>&1)"
TEST_RC=$?
set -e
if [[ "$TEST_RC" -ne 0 ]]; then
  printf '%s\n' "$TEST_OUT" | tail -60 >&2
  die "Telegram-focused preflight tests failed; production untouched"
fi
printf '%s\n' "$TEST_OUT" | tail -3
printf '%s\n' "$TEST_OUT" | grep -Eq '12 passed|[1-9][0-9]+ passed' \
  || die "pytest returned 0 but no passed-test summary was found"
printf '%s\n' "$TEST_OUT" | grep -Eq '[1-9][0-9]* (failed|error|errors)' \
  && die "pytest summary contains failure/error" || true

say "Fetch provenance-verified hardened rollout source"
sudo -u dev -H git -C "$DEV_REPO" fetch --no-tags origin \
  "+refs/heads/${SOURCE_BRANCH}:refs/remotes/origin/${SOURCE_BRANCH}" >/dev/null 2>&1 \
  || die "could not fetch hardened rollout source branch"
sudo -u dev -H git -C "$DEV_REPO" cat-file -e "${SOURCE_BLOB}^{blob}" \
  || die "reviewed rollout blob $SOURCE_BLOB is unavailable"
sudo -u dev -H git -C "$DEV_REPO" cat-file blob "$SOURCE_BLOB" >"$TMPD/base-rollout.sh"
echo "${SOURCE_SHA256}  $TMPD/base-rollout.sh" | sha256sum -c - \
  || die "hardened source rollout SHA256 mismatch"

say "Derive Telegram rollout by five exact identity substitutions"
python3 - "$TMPD/base-rollout.sh" "$PATCHED" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
replacements = [
    (
        b'BASELINE_SHA="775296e1d516279381a4c516dfb3e783b33a7495"',
        b'BASELINE_SHA="d14e28dfc63ca6e4da920229b0ab5ba0f33b93df"',
    ),
    (
        b'TARGET_SHA="d14e28dfc63ca6e4da920229b0ab5ba0f33b93df"',
        b'TARGET_SHA="e113f5c6255925f1a93f0f8c242dcd5facbaf41a"',
    ),
    (
        b'TARGET_TREE="7a795888f7794bbd7049ecd5468bf0aa3f419d86"',
        b'TARGET_TREE="6631860fdc2e79b6f66c2edb9c14207c374bce5a"',
    ),
    (
        b'EXPECTED_CHANGED_FILES=30',
        b'EXPECTED_CHANGED_FILES=6',
    ),
    (
        b'EXPECTED_FILELIST_SHA="c93082bd02df2e53439ffa7aabdd748d2b1312c5fd17acf4c2e5db24a58eb2af"',
        b'EXPECTED_FILELIST_SHA="ca760a33ea8563cb2ccf55586c94d51cc084347c357dc70adc80c92593f563f4"',
    ),
]
for old, new in replacements:
    count = data.count(old)
    if count != 1:
        raise SystemExit("expected exactly one occurrence of %r, found %d" % (old, count))
    data = data.replace(old, new)
open(dst, "wb").write(data)
PY
chown root:root "$PATCHED"
chmod 0700 "$PATCHED"

# The source script itself re-verifies: baseline identity + exact authorized
# config delta; target tree; exact changed-file list; OneCLI/Telegram/provider
# health; seven timers; recovery fix canaries; 163 deterministic recovery tests
# when pytest is present; private/read-only Mission Control; intraday override;
# and convergent rollback on any failure after mutation.
say "Execute hardened governed rollout"
exec "$PATCHED"
