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

## Second review round — four more defects, found before any rollout ran

External review then found one more real bug in the fix above, which
triggered a full adversarial self-review of the whole script (mine, plus an
independent fresh pass by a reviewer that did not author any of this). Five
things were found and fixed, all before this script was ever run against
production:

1. **Newline asymmetry in the fix itself** (the reported bug):
   `verify_intraday_override_only` read the committed content via bash
   `$(...)` command substitution, which strips every trailing newline — the
   real working file always keeps its own, so the two sides' line counts
   were off by exactly one, unconditionally, regardless of content. The
   accepted baseline would have been rejected every single time. Fixed at
   the file/stream boundary: the committed blob is now written to a real
   file (`>` redirection, not `$(...)`) in a qamc-owned scratch dir, and both
   sides are read via `open().read()` in Python — symmetric, no shell
   transformation on either side. New regression test drives the REAL shell
   function against a REAL git repo, not just the embedded Python in
   isolation (`test_real_function_accepts_the_exact_delta_with_a_trailing_newline`
   and neighbors).
2. **Block-scan gives up after one line** (found independently by both the
   self-review and the fresh-eyes review): the verifier's re-scan for the
   `enabled:` line inside `intraday_scan:` had an unconditional `break`
   after the FIRST line in the block, copy-drifted from `EDITOR_PY`'s
   structurally similar loop, whose `break` only fires inside the match
   branch. It only worked because `enabled:` happens to be literally the
   first key in the block today — a comment or reordered key ahead of it
   would have made a genuinely valid delta fail for a reason unrelated to
   production being wrong. Fixed to scan through the block exactly like
   `EDITOR_PY` does.
3. **Trailing content asymmetry, one line down** (found by the fresh-eyes
   review): `EDITOR_PY` preserves whatever follows the value on the
   `enabled:` line (an inline comment, most likely) when it flips
   false→true; the verifier's regexes were anchored to end-of-line right
   after the value, so it would reject the mutator's own correct
   true-with-comment output. Fixed to capture and compare that trailing
   content explicitly rather than anchor past it.
4. **A second interrupting signal during convergence went completely
   silent** (found by the fresh-eyes review, confirmed by direct empirical
   test of bash's own EXIT-trap-on-signal-termination behavior before
   fixing): `converge()` reset INT/TERM/HUP to default disposition rather
   than ignoring them, so a second Ctrl-C/TERM/HUP arriving while
   convergence was still running (e.g. during the up-to-90s restart/health
   wait) killed the process with the recursion guard (`IN_CONVERGE`) still
   set. `on_exit`'s belt-and-braces retry saw the guard set and returned
   immediately — a root-privileged rollback tool going silent about whether
   production converged, with no report at all. Fixed two ways: `converge()`
   now ignores (`trap ''`, not `trap -`) INT/TERM/HUP for its own duration,
   so this class of interruption cannot happen in the first place; `on_exit`
   also now checks for the guard still being set and, if so, reports loudly
   that a previous attempt was interrupted and production's actual state is
   unknown, rather than silently returning — defense in depth for any other
   signal this script does not explicitly trap. New test drives a REAL
   second `SIGTERM`, via a background job, into the middle of an in-progress
   `converge()` call, and requires the process survive and report normally.
5. Two smaller findings from the same passes: `override_rc`/
   `override_verify_rc` were assigned in `converge()` without `local`
   (harmless here, no name collision, but inconsistent with the rest of the
   file's discipline); and, while fixing #2, a possessive apostrophe in a
   prose comment landed inside the single-quoted `python3 -c '...'` string
   it documents — bash single quotes have no escape mechanism at all, so it
   silently truncated the string and left following Python text to be
   parsed as bash. Caught by `bash -n`, which is now run after every edit to
   this file rather than periodically.

Every fix has a dedicated regression test exercising the real bash
(`_real_repo_harness` against a real git repo for #1–#3, the same
stub-and-trace `converge()` harness the rest of the suite uses for #4), not
only the embedded Python or a re-reading of the diff.

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
against the two blobs. This script is 1532 lines total, versus 1262 in the
original finish-line script it derives from; growth is almost entirely the
shared editor/verifier functions, their doc comments, and the second review
round's fixes above.
Every changed region is one of: the six pinned constants; the header/phase
comments; the Gate-A baseline-delta fix and the matching convergence fix
(both directly tested — see below); the seven new fix-marker checks (added
in three places, mirroring the file's own existing defense-in-depth style
for the PR #48 markers); the Gate C test-file list, its expected count, and
its PR #48 → recovery wording; Gate D now calls the shared editor instead of
carrying its own copy (one definition, three call sites, so Gate A/Gate
D/convergence cannot silently drift apart); the four defects from the second
review round. Gate B and Gate E's provider/OneCLI/Tailscale/timer checks are
byte-identical.

`tests/test_recovery_rollout_script.py` (adapted from the finish-line
script's own `tests/test_rollout_script.py`, brought onto this branch rather
than left behind) covers this exact script. Diffed by test-function name
against the original (verified with `comm`, not estimated): of 73 original
test functions, 72 are untouched by name, 1
(`test_dirty_tree_after_rollback_is_reported`) was repurposed into two —
its old premise (a dirty tree after rollback is always wrong) was exactly
backwards once the fix landed, so it split into
`test_unexpectedly_clean_tree_after_rollback_is_reported` (the direct
regression test for the reported defect) and
`test_unexpectedly_dirty_tree_after_rollback_is_reported` (dirty-but-still-wrong
stays rejected). Two of the 72 untouched-by-name functions had their bodies
adjusted for deliberate changes: `test_state_is_set_before_the_config_edit_not_after`'s
call-site pattern (Gate D now calls the shared function) and
`test_convergence_is_recursion_safe`'s trap-string assertion (`trap ''
INT TERM HUP`, not `trap - ... INT TERM HUP`). 31 new test functions were
added across both review rounds, several parametrized across the three real
failure-injection points (`deployed`/`restarted`/`enabled`) — behavioural
tests of `verify_intraday_override_only` and `apply_intraday_override`
(exact delta, clean tree, unrelated change, cash-sweep switch, non-`true`
value, line-count mismatch, not-first-line, trailing-comment preservation),
structural tests pinning the Gate-A/Phase-3/Gate-D wiring, and functional
harness tests driving convergence's actual bash — via stubs, not just
reading the text — through every override failure mode plus a real
in-flight second `SIGTERM`. Net effect: 116 → **150 collected tests, 150/150
passed**, re-run against the exact committed bytes on this branch, not a
local copy.

## Single operator command

Run **as `ubuntu`**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent fetch --no-tags origin "+refs/heads/claude/trading-utility-recovery-rollout:refs/remotes/origin/claude/trading-utility-recovery-rollout" && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 13661bdad8d6df83dd2cee048b6d6727f3e5c582 | install -o root -g root -m 0700 /dev/stdin /root/qamc-recovery-rollout.sh && echo "f77d7ee2512016a9fb0cf718a6dc4877c0e2077a771cf0ac2af80c7521417712  /root/qamc-recovery-rollout.sh" | sha256sum -c - && /root/qamc-recovery-rollout.sh'
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
