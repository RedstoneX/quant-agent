# QAMC trading-utility recovery rollout — operator handoff

This runbook deploys the externally reviewed trading-utility recovery (PR #56,
merged to `main`) and verifies Gates A→E on the deployed tree. It never
manufactures a trade. It is a direct derivative of
`ops/review/qamc-finish-line-rollout.sh` (the PR #48 / finish-line rollout,
externally reviewed and already run once in production): Gate A, B, D, E, the
deployment-state machine, convergent rollback and every self-integrity check
are reused verbatim. Only the baseline/target identity, the Gate C focused
suite, and one new content-verification block (the seven trading-utility
recovery fix markers) are new.

## Pinned identities

| Item | Value |
|---|---|
| Production rollback point (current production) | `775296e1d516279381a4c516dfb3e783b33a7495` |
| Deployment target (`main`, PR #56 merged) | `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` |
| Target tree | `7a795888f7794bbd7049ecd5468bf0aa3f419d86` |
| Baseline→target changed files | `30` |
| Changed-file-list SHA-256 | `c93082bd02df2e53439ffa7aabdd748d2b1312c5fd17acf4c2e5db24a58eb2af` |
| Gate C focused-suite expected count | `163 passed, 0 failed/error/skipped/xfailed` |
| Full branch suite (evidentiary, not gated) | `1997 passed, 0 failed` |

All of the above were computed directly against the actual `d14e28d` commit
content on the `dev` account (`git diff --name-only`, `sha256sum`, and a live
`pytest` run in an isolated `git worktree`), not copied from a claim.

## Why this is a new script rather than a launcher re-pin

The previous rollout's launcher pattern (`qamc-finish-line-launcher.sh`)
worked because only five literal values needed to change and the baseline
never moved. Here the baseline genuinely moves (production has advanced from
`9c736c1` all the way to `775296e1` since that pattern was last used), Gate
C's focused suite must target different test files entirely (the trading-
utility recovery touches different code than PR #48 did), and a new class of
evidence — the seven recovery fix markers — needed adding. A five-line patch
could not honestly represent that. Instead, this script is the reviewed
source **plus a bounded, fully-listed diff**, verifiable the same way the
original was reviewed: read the diff below, or run
`diff ops/review/qamc-finish-line-rollout.sh ops/review/qamc-recovery-rollout.sh`
against the two blobs. The diff is 157 changed lines out of 1337 total, and
every changed region is one of: the six pinned constants; the header/phase
comments; the seven new fix-marker checks (added in three places, mirroring
the file's own existing defense-in-depth style for the PR #48 markers); the
Gate C test-file list, its expected count, and its PR #48 → recovery wording.
Gate A, Gate B, the deployment-state machine, convergent rollback, every
self-integrity check, and Gate D's config editor are byte-identical.

The full existing structural test suite for this script family
(`tests/test_rollout_script.py`, 116 tests — static safety, deployment-state
ordering, convergence, self-permission checks, the config editor, the C3
SGOV/liquidity fixture, the Gate-C count parser, the Gate-E awk/listener
checks) was run against this exact script and **passed 116/116**.

## Single operator command

Run **as `ubuntu`**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent fetch --no-tags origin "+refs/heads/claude/trading-utility-recovery-rollout:refs/remotes/origin/claude/trading-utility-recovery-rollout" && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 51d16b7e29c2015aea337aa795f154ce6e6c2b16 | install -o root -g root -m 0700 /dev/stdin /root/qamc-recovery-rollout.sh && echo "87ee66f3e3c7b9676130d9e145385be75cf3caa9f510a2c603d6cebc2937d18d  /root/qamc-recovery-rollout.sh" | sha256sum -c - && /root/qamc-recovery-rollout.sh'
```

The command fetches the review branch **without touching the dev working
tree**, reads the exact reviewed script by immutable Git blob id (not by
mutable file path — nothing on disk between review and execution can change
what runs), installs it root-owned mode `0700`, verifies its sha256 against
the reviewed hash printed above, and only then executes it. The script prints
its own transcript to `/root/qamc-rollout-<UTC timestamp>.log` (root-only,
0600) as it runs.

## Expected sequence

- Gate A: baseline/runtime/timers/wrappers/health — nothing changes yet.
- Fetch + verify target commit content: exact tree, exact 30-file delta, all
  five inherited PR #48 markers, all seven new recovery fix markers, paper-only,
  intraday still `false` in the committed config.
- Deploy exact `d14e28d...` detached; import/config smoke.
- Restart Mission Control onto the deployed code.
- Gate B: OneCLI, exact-host Tailscale privacy, providers, model routing,
  Alpaca Paper, FRED, DB, Telegram, seven timers.
- Gate C: the seven recovery fix markers re-confirmed + focused test suite
  (163 passed expected) + live SGOV/liquidity reconciliation + inherited
  batch-completeness markers.
- Gate D: read-only intraday snapshot smoke, then re-apply the one-line local
  override (`intraday_scan.enabled: false -> true`) — the same edit
  production already carries; the committed default is still `false`.
- Gate E: adversarial final acceptance, including a third confirmation of all
  seven recovery markers after enablement.

Success ends with:

`GATE E / FINISH LINE PASSED`

Any failure after deployment invokes the existing convergent rollback to
`775296e1...`, restores config, restarts Mission Control, and reports whether
convergence completed.

## Closeout

Return the transcript. `docs/STATE.md` and `docs/WORK.md` are updated from the
actual production evidence, not from this runbook.
