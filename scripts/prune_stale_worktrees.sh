#!/usr/bin/env bash
# Extended worktree cleanup: removes stale registrations AND abandoned scratch.
#
# Conservative approach based on measured discriminators:
#   - MERGED branches: remove regardless of age (safe; PR already landed)
#   - UNMERGED branches with ACTIVE REMOTES: keep, even if old (active work)
#   - UNMERGED with GONE remotes: remove if 7+ days old (abandoned PRs)
#   - Require clean working tree (no uncommitted/untracked files) always
#
# Never touches /home/ubuntu/worktrees/ (active sessions) or other tenants.
# Measurement: 2 merged branches (b143, b174) are safe to remove now;
# 48 unmerged with active remotes should be kept (ages 0-4 days, pending).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MIN_AGE_SECS=$((7 * 86400))  # 7 days: secondary guard for abandoned unmerged PRs
NOW=$(date +%s)

cd "$PROJECT_ROOT"

# First: standard git worktree prune (removes registrations with missing dirs).
echo "==> Pruning stale registrations (missing directories)..."
git worktree prune --verbose 2>&1 | head -20 || true

# Second: collect and remove merged/abandoned scratch.
echo "==> Scanning for finished scratch worktrees (merged or remote gone)..."

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
  
  # Check if working tree is clean (no uncommitted/untracked files).
  status=$(cd "$wt_path" 2>/dev/null && git status --porcelain 2>/dev/null | wc -l || echo 999)
  [ "$status" -eq 0 ] || continue  # Skip if dirty
  
  # PRIMARY DISCRIMINATOR: Is the branch already merged into origin/main?
  if (cd "$wt_path" 2>/dev/null && git merge-base --is-ancestor HEAD origin/main 2>/dev/null); then
    # Merged: safe to remove regardless of age.
    echo "  Removing: $(basename "$wt_path") (merged into main)"
    size=$(du -sb "$wt_path" 2>/dev/null | awk '{print $1}' || echo 0)
    rm -rf "$wt_path" 2>/dev/null && git worktree remove "$wt_path" --force 2>/dev/null || true
    bytes_freed=$((bytes_freed + size))
    count_removed=$((count_removed + 1))
    continue
  fi
  
  # SECONDARY DISCRIMINATOR: Is the remote branch gone?
  branch=$(cd "$wt_path" 2>/dev/null && git branch --show-current 2>/dev/null || echo "unknown")
  if ! (cd "$wt_path" 2>/dev/null && git rev-parse --verify "origin/$branch" >/dev/null 2>&1); then
    # Remote gone: branch was deleted (likely force-pushed or PR deleted).
    # Only remove if also old enough (7+ days) to confirm truly abandoned.
    mtime=$(stat -c %Y "$wt_path" 2>/dev/null || echo 0)
    age_secs=$((NOW - mtime))
    [ "$age_secs" -ge "$MIN_AGE_SECS" ] || continue
    
    echo "  Removing: $(basename "$wt_path") (remote gone, $((age_secs / 86400)) days old)"
    size=$(du -sb "$wt_path" 2>/dev/null | awk '{print $1}' || echo 0)
    rm -rf "$wt_path" 2>/dev/null && git worktree remove "$wt_path" --force 2>/dev/null || true
    bytes_freed=$((bytes_freed + size))
    count_removed=$((count_removed + 1))
  fi
done < "$tmpfile"

# Report what was cleaned (for monitoring).
if command -v numfmt &>/dev/null; then
  freed_readable=$(numfmt --to=iec-i --suffix=B "$bytes_freed")
else
  freed_readable="${bytes_freed} bytes"
fi
echo "==> Cleanup summary: removed $count_removed worktrees, freed $freed_readable (skipped $count_skipped)"

