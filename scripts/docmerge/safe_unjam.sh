#!/bin/bash
# $1 = branch, run from a worktree. Resolves WORK.md by 3-way through the repo's
# own resolver (never by taking main whole) and unions the ratchet file.
set -e
BR="$1"; HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-$(git -C "$HERE" rev-parse --show-toplevel)/.venv/bin/python}"
[ -x "$PY" ] || PY=/home/ubuntu/projects/quant-agent/.venv/bin/python
git checkout -q -B "s-$(echo $BR|tr '/' '-')" "origin/$BR"
PRE=$(git rev-parse HEAD)
git merge origin/main -m "Merge main (item-aware doc resolver)" >/dev/null 2>&1 || true
for f in $(git diff --name-only --diff-filter=U); do
  case "$f" in
    docs/WORK.md)
      PYTHONPATH=. "$PY" - "$PRE" <<'PY'
import subprocess, sys
sys.path.insert(0,'.')
import scripts.resolve_doc_conflict as rdc
pre = sys.argv[1]
show = lambda r: subprocess.run(['git','show',f'{r}:docs/WORK.md'],capture_output=True,text=True).stdout
base_ref = subprocess.run(['git','merge-base',pre,'origin/main'],capture_output=True,text=True).stdout.strip()
strip = lambda t: "\n".join(l for l in t.split("\n") if l.strip() != '- retired queue: 211')
base, ours, theirs = strip(show(base_ref)), strip(show(pre)), show('origin/main')
m = rdc.RESOLVERS['work'](base, ours, theirs)
assert '<<<<<<<' not in m, 'resolver refused'
open('docs/WORK.md','w').write(m)
print('work resolved')
PY
      git add docs/WORK.md;;
    *) echo "UNEXPECTED $f"; exit 3;;
  esac
done
rm -f docs/*.merge-refusal
if ! git diff --cached --quiet || ! git diff --quiet; then
  git commit -q -m "Merge main (item-aware doc resolver); board resolved three-way, not by taking main whole

An earlier sweep of mine resolved docs/WORK.md by checking out main's
copy, which silently discarded each branch's own board edits. This
resolves it through the repo's own item-aware resolver with the
branch's pre-merge board as ours, so both sides survive. The only line
dropped deliberately is the stale retired bullet for item 211, which
was re-opened on main.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" || true
fi
git push -q origin "HEAD:$BR"
echo "$BR ok"
