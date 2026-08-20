# QAMC finish-line rollout — final operator handoff

This runbook deploys the accepted paper-production target, verifies Gates A→E,
and enables the already-authorized intraday scanner only after the preceding
gates pass. It never manufactures a trade.

## Pinned identities

| Item | Value |
|---|---|
| Production rollback baseline | `9c736c158fec84129765c25a9429254d3602ad6b` |
| Deployment target (`main`, after PR #54) | `775296e1d516279381a4c516dfb3e783b33a7495` |
| Target tree | `988cdbffb469c1a48737b9a2db876b05b29e2f90` |
| Direct baseline→target changed files | `23` |
| Direct changed-file-list SHA-256 | `5eca9bcc68b10d6f1bc1222c57d456b8b3c0c3004b26cddff116b031c3bda358` |
| Reviewed inner rollout source blob | `69e0968f6438be0d7d1a5d92d4f0e5e335ba42fe` |
| Inner source SHA-256 | `77701bb2c41db5c8f7b35b813e83bf46069767d962dfaab1d75aa6bb440bbc10` |
| Inner source size | `75298` bytes |
| Final launcher Git blob | `fd8879b7a745165022888e8c4a5c26710ffa0646` |
| Final launcher SHA-256 | `2e8f01eaf1c1ecdb3112ae49b92372a2f56c1815df2c866797c073e46b626699` |
| Final launcher size | `5379` bytes |

PR #54 merged only the durable Tailscale listener-privacy verifier and its
tests. The deployment target therefore contains the corrected Gate-B
instrument without pulling temporary `ops/review/*` artifacts into production.

## Why there is a launcher

The large rollout script was already externally reviewed before PR #54. Rather
than rewrite/re-review 75 KB merely to change target metadata, the launcher:

1. reads the exact reviewed rollout source by Git blob id as `dev`;
2. changes exactly five reviewed lines:
   - target commit;
   - target tree;
   - direct changed-file count;
   - direct changed-file-list SHA-256;
   - one stale evidence-count message, replaced with count-free exact-tree wording;
3. refuses to continue unless exactly those five lines and no others changed;
4. installs the derived inner script root-owned mode `0700`;
5. executes the inner rollout, whose own fail-closed gates and convergence
   rollback remain unchanged.

This is reproducible provenance: reviewed source blob + five explicit,
fail-closed substitutions.

## Single operator command

Run **as `ubuntu`**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob fd8879b7a745165022888e8c4a5c26710ffa0646 | install -o root -g root -m 0700 /dev/stdin /root/qamc-finish-line-launcher.sh && echo "2e8f01eaf1c1ecdb3112ae49b92372a2f56c1815df2c866797c073e46b626699  /root/qamc-finish-line-launcher.sh" | sha256sum -c - && /root/qamc-finish-line-launcher.sh'
```

Nothing else is required. The launcher prints its derived inner SHA-256 and
then the rollout writes the complete transcript to
`/root/qamc-rollout-<UTC timestamp>.log`.

## Expected sequence

- Gate A: baseline/runtime/timers/wrappers/health.
- Target fetch and exact tree + 23-file delta verification.
- Deploy exact `775296e1...` detached.
- Restart Mission Control.
- Gate B: OneCLI, exact-host Tailscale privacy, providers, model routing,
  Alpaca Paper, FRED, DB, Telegram, seven timers.
- Gate C: SGOV funding semantics and Tech batch completeness.
- Gate D: read-only intraday market-data smoke, then one-line enablement.
- Gate E: adversarial final acceptance, including the same deployed
  `listener_privacy_verdict()` used by Gate B.

Success ends with:

`GATE E / FINISH LINE PASSED`

Any failure after deployment invokes the existing convergent rollback to
baseline `9c736c1`, restores config, restarts Mission Control, and reports
whether convergence completed.

## Closeout

Return the transcript. `docs/STATE.md` and `docs/WORK.md` are updated from the
actual production evidence, not from this runbook.
