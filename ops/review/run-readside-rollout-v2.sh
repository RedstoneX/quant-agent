#!/usr/bin/env bash
# Thin immutable wrapper around the previously reviewed read-side rollout.
# Retargets it to merged PR #63 and adds live acceptance for all chart timeframes.
set -Eeuo pipefail

REPO=/home/qamc/quant-agent
QAMC=qamc
BASE=e113f5c6255925f1a93f0f8c242dcd5facbaf41a
TARGET=2b3faaf69c0b842a08f991a9ca517a3989bdaf93
TREE=c75eb6b7a06c87b1743b82230e62dc5a221cda12
SOURCE_BLOB=517de90412e8cb3607add69a26909810fa3bf1e8
OLD_BRANCH=ops/readside-production-convergence
OLD_PATH=ops/review/qamc-readside-rollout.sh
RUN=/root/qamc-readside-rollout-pr63.sh
API=http://127.0.0.1:8800

[[ $(id -u) -eq 0 ]] || { echo 'run as root' >&2; exit 1; }
qgit(){ sudo -u "$QAMC" -H git -C "$REPO" "$@"; }

qgit fetch --no-tags origin \
  "+refs/heads/${OLD_BRANCH}:refs/remotes/origin/${OLD_BRANCH}" \
  "+refs/heads/main:refs/remotes/origin/main"
qgit cat-file -e "${TARGET}^{commit}"
[[ $(qgit show -s --format=%T "$TARGET") == "$TREE" ]] || { echo 'target tree mismatch' >&2; exit 1; }
qgit show "origin/${OLD_BRANCH}:${OLD_PATH}" > "$RUN"
[[ $(git hash-object "$RUN") == "$SOURCE_BLOB" ]] || { echo 'reviewed rollout source blob mismatch' >&2; exit 1; }

COUNT=$(qgit diff --name-only "$BASE" "$TARGET" | grep -c . || true)
LIST_SHA=$(qgit diff --name-only "$BASE" "$TARGET" | sha256sum | awk '{print $1}')

python3 - "$RUN" "$TARGET" "$TREE" "$COUNT" "$LIST_SHA" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); s=p.read_text()
vals={
 'TARGET_SHA':sys.argv[2], 'TARGET_TREE':sys.argv[3],
 'EXPECTED_CHANGED_FILES':sys.argv[4], 'EXPECTED_FILELIST_SHA':sys.argv[5],
}
for key,val in vals.items():
    pat=rf'^{key}="[^"]*"$' if key!='EXPECTED_CHANGED_FILES' else rf'^{key}=\d+$'
    repl=f'{key}="{val}"' if key!='EXPECTED_CHANGED_FILES' else f'{key}={val}'
    s,n=re.subn(pat,repl,s,count=1,flags=re.M)
    if n!=1: raise SystemExit(f'could not patch {key}')
p.write_text(s)
PY
chown root:root "$RUN"; chmod 0700 "$RUN"
bash -n "$RUN"

"$RUN"

for spec in '5m:1' '15m:5' '1h:30' '1d:5'; do
  tf=${spec%%:*}; days=${spec##*:}
  if ! curl -fsS --max-time 30 "${API}/prices/SPY?lookback_days=${days}&timeframe=${tf}" \
    | python3 -c 'import json,sys; tf=sys.argv[1]; d=json.load(sys.stdin); assert not d.get("error"),d.get("error"); assert d.get("symbol")=="SPY"; assert d.get("timeframe")==tf; assert len(d.get("bars") or [])>0' "$tf"; then
      echo "[FAIL] deployed chart timeframe ${tf} failed live acceptance" >&2
      echo "Core rollout succeeded but PR #63 chart acceptance did not; stop and report this output." >&2
      exit 2
  fi
done

echo
echo '== PR #63 PRODUCTION CONVERGENCE PASSED =='
echo "Production SHA: $TARGET"
echo 'Chart: 5m Today / 15m / 1h / 1D PASS'
echo 'Trading: Alpaca Paper; Mission Control remains GET-only'
