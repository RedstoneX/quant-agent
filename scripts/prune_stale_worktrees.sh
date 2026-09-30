#!/usr/bin/env bash
# Extended worktree cleanup: removes stale registrations AND abandoned scratch.
#
# Conservative approach: only removes /tmp session-scratch worktrees that are:
#   - 5+ days old (measured session lifetime: 1-2 days; 5d = definite abandon)
#   - Clean (no uncommitted/untracked files)
#   - Merged into origin/main (safe to discard)
#   - Owned by ubuntu (safety: shared box with other tenants)
#
# NEVER touches /home/ubuntu/worktrees/ (active sessions) or other tenants.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MIN_AGE_SECS=$((5 * 86400))  # 5 days: observed session lifetime is 1-2 days
NOW=$(date +%s)

cd "$PROJECT_ROOT"

# First: standard git worktree prune (removes registrations with missing dirs).
echo "==> Pruning stale registrations (missing directories)..."
git worktree prune --verbose 2>&1 | head -20 || true

# Second: collect and remove clean/merged /tmp scratch older than 5 days.
echo "==> Scanning for abandoned session-scratch worktrees (5+ days old)..."

bytes_freed=0
count_removed=0
count_skipped=0

# Build a temp file of /tmp worktrees to process (avoid subshell variable loss).
tmpfile=$(mktemp)
trap "rm -f $tmpfile" EXIT

git worktree list --porcelain 2>/dev/null | grep "^/tmp/" | awk '{print $1}' > "$tmpfile" || true

while IFS= read -r wt_path; do
  [ -z "$wt_path" ] && continue
  
  # Sanity: does it still exist?
  [ -d "$wt_path" ] || { git worktree remove "$wt_path" --force 2>/dev/null || true; continue; }
  
  # Safety: only touch ubuntu-owned paths (not another tenant's).
  owner=$(stat -c %U "$wt_path" 2>/dev/null || echo "unknown")
  [ "$owner" = "ubuntu" ] || { count_skipped=$((count_skipped + 1)); continue; }
  
  # Check age: must be 5+ days old to be considered abandoned.
  mtime=$(stat -c %Y "$wt_path" 2>/dev/null || echo 0)
  age_secs=$((NOW - mtime))
  age_days=$((age_secs / 86400))
  [ "$age_secs" -ge "$MIN_AGE_SECS" ] || continue
  
  # Check if working tree is clean.
  if ! (cd "$wt_path" && git status --porcelain 2>/dev/null | grep -q .); then
    # Check if merged: if HEAD is an ancestor of origin/main, it's safe to discard.
    if (cd "$wt_path" && git merge-base --is-ancestor HEAD origin/main 2>/dev/null); then
      echo "  Removing: $(basename "$wt_path") (${age_days}d old, merged into main)"
      size=$(du -sb "$wt_path" 2>/dev/null | awk '{print $1}' || echo 0)
      rm -rf "$wt_path" 2>/dev/null && git worktree remove "$wt_path" --force 2>/dev/null || true
      bytes_freed=$((bytes_freed + size))
      count_removed=$((count_removed + 1))
    fi
  fi
done < "$tmpfile"

# Report what was cleaned (for monitoring).
if command -v numfmt &>/dev/null; then
  freed_readable=$(numfmt --to=iec-i --suffix=B "$bytes_freed")
else
  freed_readable="${bytes_freed} bytes"
fi
echo "==> Cleanup summary: removed $count_removed worktrees, freed $freed_readable (skipped $count_skipped)"

