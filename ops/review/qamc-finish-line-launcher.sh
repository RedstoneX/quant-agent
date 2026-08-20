#!/usr/bin/env bash
#
# QAMC finish-line launcher — reproducibly re-pins the already-reviewed rollout
# script to the durable main commit that contains the corrected Tailscale
# listener-privacy verifier, then executes the resulting root-owned script.
#
# This launcher changes NO production state before the inner rollout script
# begins. It fails closed unless:
#   * it is root-owned mode 0700;
#   * the reviewed source script is read by exact Git blob id;
#   * every expected old literal occurs exactly once;
#   * exactly the five reviewed lines below change and no others;
#   * the derived inner script is root-owned mode 0700.
#
set -Eeuo pipefail

SOURCE_REPO="/home/dev/projects/quant-agent"
SOURCE_BLOB="69e0968f6438be0d7d1a5d92d4f0e5e335ba42fe"
INNER="/root/qamc-finish-line-rollout-inner.sh"

OLD_TARGET_SHA="bb223eadde30654d72ab11e055185a757d0cddc0"
NEW_TARGET_SHA="775296e1d516279381a4c516dfb3e783b33a7495"

OLD_TARGET_TREE="ff27c9458ba6f4677c8db2329af7d8d47b176e77"
NEW_TARGET_TREE="988cdbffb469c1a48737b9a2db876b05b29e2f90"

OLD_FILE_COUNT="EXPECTED_CHANGED_FILES=21"
NEW_FILE_COUNT="EXPECTED_CHANGED_FILES=23"

OLD_FILELIST_SHA='EXPECTED_FILELIST_SHA="bc872bac49c3abea63128f907c80445e6156902bdbb2279693c739016b98af11"'
NEW_FILELIST_SHA='EXPECTED_FILELIST_SHA="5eca9bcc68b10d6f1bc1222c57d456b8b3c0c3004b26cddff116b031c3bda358"'

OLD_C1='ok "C1 deployed tree == reviewed tree $TARGET_TREE (reviewed full suite: 1925 passed, 0 failed)"'
NEW_C1='ok "C1 deployed tree == reviewed tree $TARGET_TREE; exact-tree deterministic evidence transferred"'

err() { printf '[LAUNCHER FAIL] %s\n' "$*" >&2; exit 1; }
ok()  { printf '[LAUNCHER OK] %s\n' "$*"; }

[[ "$(id -u)" -eq 0 ]] || err "run as root"

SELF="$(readlink -f "$0")"
read -r OWNER GROUP MODE <<<"$(stat -c '%U %G %a' "$SELF")"
[[ "$OWNER" == root && "$GROUP" == root && "$MODE" == 700 ]] \
  || err "launcher must be root:root mode 700 (got $OWNER:$GROUP $MODE)"

TMP="$(mktemp /root/qamc-finish-line-source.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

# Read the exact reviewed bytes from Git's content-addressed object store as dev.
# pipefail is already enabled: a failed cat-file cannot silently become an
# empty-but-successfully-installed file.
sudo -u dev -H git -C "$SOURCE_REPO" cat-file blob "$SOURCE_BLOB" >"$TMP" \
  || err "could not read reviewed source blob $SOURCE_BLOB"
[[ -s "$TMP" ]] || err "reviewed source blob unexpectedly empty"

python3 - "$TMP" "$INNER" \
  "$OLD_TARGET_SHA" "$NEW_TARGET_SHA" \
  "$OLD_TARGET_TREE" "$NEW_TARGET_TREE" \
  "$OLD_FILE_COUNT" "$NEW_FILE_COUNT" \
  "$OLD_FILELIST_SHA" "$NEW_FILELIST_SHA" \
  "$OLD_C1" "$NEW_C1" <<'PY'
from pathlib import Path
import sys

(src_path, out_path,
 old_sha, new_sha,
 old_tree, new_tree,
 old_count, new_count,
 old_list_hash, new_list_hash,
 old_c1, new_c1) = sys.argv[1:]

src = Path(src_path).read_text()
replacements = [
    (f'TARGET_SHA="{old_sha}"', f'TARGET_SHA="{new_sha}"'),
    (f'TARGET_TREE="{old_tree}"', f'TARGET_TREE="{new_tree}"'),
    (old_count, new_count),
    (old_list_hash, new_list_hash),
    (old_c1, new_c1),
]

derived = src
for old, new in replacements:
    n = derived.count(old)
    if n != 1:
        raise SystemExit(f"expected old literal exactly once, found {n}: {old[:120]}")
    derived = derived.replace(old, new, 1)

before = src.splitlines()
after = derived.splitlines()
if len(before) != len(after):
    raise SystemExit("re-pin unexpectedly changed line count")

changes = [(i + 1, a, b) for i, (a, b) in enumerate(zip(before, after)) if a != b]
if len(changes) != len(replacements):
    raise SystemExit(f"expected exactly {len(replacements)} changed lines, got {len(changes)}")

# Assert each changed line is one of the reviewed old->new pairs; nothing else.
for line_no, old_line, new_line in changes:
    # TARGET_SHA / TARGET_TREE lines contain comments after the literal; compare
    # by exact single replacement rather than whole-line equality.
    matched = any(old in old_line and new in new_line and
                  old_line.replace(old, new, 1) == new_line
                  for old, new in replacements)
    if not matched:
        raise SystemExit(f"unexpected change at line {line_no}: {old_line!r} -> {new_line!r}")

out = Path(out_path)
out.write_text(derived)
print("changed_lines=" + ",".join(str(n) for n, _, _ in changes))
PY

chown root:root "$INNER"
chmod 0700 "$INNER"
read -r IOWNER IGROUP IMODE <<<"$(stat -c '%U %G %a' "$INNER")"
[[ "$IOWNER" == root && "$IGROUP" == root && "$IMODE" == 700 ]] \
  || err "derived rollout script ownership/mode wrong"

# Last structural assertions on the derived artifact before execution.
grep -qF "TARGET_SHA=\"$NEW_TARGET_SHA\"" "$INNER" \
  || err "derived target SHA missing"
grep -qF "TARGET_TREE=\"$NEW_TARGET_TREE\"" "$INNER" \
  || err "derived target tree missing"
grep -qF "$NEW_FILE_COUNT" "$INNER" \
  || err "derived changed-file count missing"
grep -qF "$NEW_FILELIST_SHA" "$INNER" \
  || err "derived changed-file-list hash missing"

INNER_SHA256="$(sha256sum "$INNER" | awk '{print $1}')"
INNER_SIZE="$(stat -c '%s' "$INNER")"
ok "reviewed source blob $SOURCE_BLOB reproduced and re-pinned by exactly five line substitutions"
ok "target $NEW_TARGET_SHA / tree $NEW_TARGET_TREE / 23-file direct delta"
ok "derived inner sha256=$INNER_SHA256 size=$INNER_SIZE bytes"
ok "executing derived rollout"

exec "$INNER"
