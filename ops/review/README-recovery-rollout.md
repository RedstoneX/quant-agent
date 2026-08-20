# QAMC trading-utility recovery rollout — operator handoff

This runbook deploys the externally reviewed trading-utility recovery (PR #56,
merged to `main`) and verifies Gates A→E on the deployed tree. It never
manufactures a trade. It is a direct derivative of
`ops/review/qamc-finish-line-rollout.sh` (the PR #48 / finish-line rollout,
externally reviewed and already run once in production): Gate B, the
deployment-state machine, convergent rollback and every self-integrity check
are reused, structurally unchanged. Gate A, Phase 3 and convergence were
corrected for a real defect an external reviewer found in the first draft of
this script (see "Baseline-state defect" below) — that correction, the
baseline/target identity, the Gate C focused suite, and one new
content-verification block (the seven trading-utility recovery fix markers)
are what's new.

## Baseline-state defect (found and fixed before any rollout ran)

Current production is not a clean checkout of `775296e1...`. Per
`docs/STATE.md` it is that commit **plus exactly one authorized local delta**:
`config/settings.yaml`, `intraday_scan.enabled: false -> true`. The first
draft of this script inherited its baseline-handling verbatim from the
finish-line script — correct there, because *that* script's baseline
(`9c736c1`) really was clean; the local delta was Gate D's first-ever
enablement, not yet present at Gate A. Re-pinning `BASELINE_SHA` to
`775296e1` without re-examining that assumption left two bugs:

- **Gate A** required `git status --porcelain` to be empty — the accepted
  baseline never satisfies that, so the script would have refused to start.
- **Rollback** ran `git checkout -- config/settings.yaml` (discard to
  committed content) then checked out `BASELINE_SHA` — landing production on
  intraday **disabled** after any failure past Phase 3, silently violating the
  "intraday stays enabled" boundary.

Fix: Gate A now verifies the tree is byte-identical to the committed baseline
**except** for exactly that one line (`verify_intraday_override_only`,
read-only), and rejects both an unexpectedly clean tree and any dirty state
that isn't precisely that delta. Phase 3 discards the local delta immediately
before checking out the target (a tracked, converge-covered mutation, not a
Gate-A side effect) so the rest of the pipeline still runs against a
genuinely clean tree exactly as reviewed. Convergence re-applies the same
authorized delta (`apply_intraday_override`, the same editor Gate D uses —
defined once, shared by both) after restoring the baseline checkout, and
independently re-verifies it before ever reporting `PRODUCTION CONVERGED`.
Every failure/rollback path now restores `775296e1` **plus** the override,
not bare baseline.

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
against the two blobs. The diff is 354 changed lines out of 1442 total. Every
changed region is one of: the six pinned constants; the header/phase
comments; the Gate-A baseline-delta fix and the matching convergence fix
described above (both new, both directly tested — see below); the seven new
fix-marker checks (added in three places, mirroring the file's own existing
defense-in-depth style for the PR #48 markers); the Gate C test-file list,
its expected count, and its PR #48 → recovery wording; Gate D now calls the
shared editor instead of carrying its own copy (one definition, three call
sites, so Gate A/Gate D/convergence cannot silently drift apart). Gate B and
Gate E's provider/OneCLI/Tailscale/timer checks are byte-identical.

`tests/test_recovery_rollout_script.py` (adapted from the finish-line
script's own `tests/test_rollout_script.py`, brought onto this branch rather
than left behind) covers this exact script. Diffed by test-function name
against the original: 114 of 116 untouched; 1 (`test_dirty_tree_after_rollback_is_reported`)
repurposed into two — its old premise (a dirty tree after rollback is always
wrong) was exactly backwards once the fix landed, so it split into
`test_unexpectedly_clean_tree_after_rollback_is_reported` (the direct
regression test for the reported defect) and
`test_unexpectedly_dirty_tree_after_rollback_is_reported` (dirty-but-still-wrong
stays rejected); 1 (`test_state_is_set_before_the_config_edit_not_after`)
had its call-site pattern updated for Gate D now calling the shared function.
19 more new test functions were added (2 of them parametrized across the
three real failure-injection points, `deployed`/`restarted`/`enabled`): 6
direct behavioural tests of `verify_intraday_override_only` (accepts the
exact delta; rejects clean, a second unrelated change, the cash-sweep switch
flipped instead, a non-`true` value, a line-count mismatch), 6 structural
tests pinning the Gate-A/Phase-3/Gate-D wiring, and 9 functional harness
tests driving convergence's actual bash — via stubs, not just reading the
text — through the override-missing, override-corrupted,
override-apply-failed and override-verify-failed cases. Net effect: 116 →
139 collected tests. **139/139 passed.**

## Single operator command

Run **as `ubuntu`**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent fetch --no-tags origin "+refs/heads/claude/trading-utility-recovery-rollout:refs/remotes/origin/claude/trading-utility-recovery-rollout" && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 38263ccfdcf503873e377c96da30041ecb6b443f | install -o root -g root -m 0700 /dev/stdin /root/qamc-recovery-rollout.sh && echo "5bd50739bf5f6d38a59f2ce705dcdfea1f05f0e6c8bfa35dddcf91aa282b8d30  /root/qamc-recovery-rollout.sh" | sha256sum -c - && /root/qamc-recovery-rollout.sh'
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
  Verifies the tree is `775296e1` plus **exactly** the authorized intraday
  delta (not clean, not dirty any other way) before touching anything.
- Fetch + verify target commit content: exact tree, exact 30-file delta, all
  five inherited PR #48 markers, all seven new recovery fix markers, paper-only,
  intraday still `false` in the committed config.
- Deploy: discard the local delta (clean baseline), checkout exact `d14e28d...`
  detached, import/config smoke. [MUTATION, tracked by the state machine from
  this point on]
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

Any failure from this point past the deploy step invokes the existing
convergent rollback: checkout discarded, `775296e1` re-checked out, the
authorized intraday override **re-applied and independently re-verified**
(not just the committed baseline), Mission Control restarted, health
re-confirmed. `PRODUCTION CONVERGED` is only ever printed once all of that —
override included — is confirmed true; otherwise `CONVERGENCE INCOMPLETE`
with exact by-hand recovery commands.

## Closeout

Return the transcript. `docs/STATE.md` and `docs/WORK.md` are updated from the
actual production evidence, not from this runbook.
