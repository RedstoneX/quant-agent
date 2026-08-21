# QAMC read-side production convergence — operator handoff

This rollout advances production from the deployed trading-utility recovery
`d14e28dfc63ca6e4da920229b0ab5ba0f33b93df` to the exact reviewed current
`main` target `52c19c3912504412f47e4088e48c9b3b6296ba0c`.

The target contains the already-accepted Telegram trader-feed enrichment and
PR #60 Mission Control professional cockpit/data-truth integration, followed
only by documentation reconciliation commits. Production remains Alpaca Paper.

The rollout is fail-closed. It requires production to start at the exact
baseline plus exactly one local delta (`intraday_scan.enabled: true`), pins the
target commit tree and exact 82-file baseline→target file list, refuses an
active QAMC session, runs focused deterministic tests, restarts only Mission
Control, reapplies the authorized intraday override, and verifies providers,
Telegram, timers, read-only API behavior, current quote/history separation and
final production identity. Any failure after checkout attempts convergence back
to the baseline plus the same intraday override and restarts Mission Control.

It never runs a trading mode, never submits/cancels/modifies an order, never
changes timers/services/network/security/OneCLI, and never sends a Telegram
message (`getMe` only).

## Reviewed identities

- Baseline SHA: `d14e28dfc63ca6e4da920229b0ab5ba0f33b93df`
- Target SHA: `52c19c3912504412f47e4088e48c9b3b6296ba0c`
- Target tree: `226469380ca38dbcae5c2d31c3b55162ed2f0ed3`
- Changed files: `82`
- Changed-file-list SHA-256: `ce341de01ef91f6789f4bce52cb5978d7a67738529a5e7b96e25cb986cd68988`
- Rollout-script Git blob: `364b8e168b81562395304a2909fab38e6672c93e`
- Rollout-script SHA-256: `975e8f0e967dc6ed3af4fb7376c705c296b32212ee9fe735af4f871ca9924e3a`

The rollout script was syntax-checked with `bash -n` before publication.

## Single operator command

Run this **as `ubuntu` on the VPS**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent fetch --no-tags origin "+refs/heads/ops/readside-production-convergence:refs/remotes/origin/ops/readside-production-convergence" && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 364b8e168b81562395304a2909fab38e6672c93e | install -o root -g root -m 0700 /dev/stdin /root/qamc-readside-rollout.sh && echo "975e8f0e967dc6ed3af4fb7376c705c296b32212ee9fe735af4f871ca9924e3a  /root/qamc-readside-rollout.sh" | sha256sum -c - && /root/qamc-readside-rollout.sh'
```

The script writes a root-only transcript to
`/root/qamc-readside-rollout-<UTC timestamp>.log`.

Success ends with `FINISH LINE PASSED` and prints the exact production SHA,
local intraday delta, Mission Control/Telegram/timer verdicts and transcript
path. Return that output/transcript for final state reconciliation and the
operator-side desktop/iPad visual check.
