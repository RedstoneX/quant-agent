# QAMC read-side production convergence — operator handoff

This rollout advances the **observed production baseline**
`e113f5c6255925f1a93f0f8c242dcd5facbaf41a` (Telegram trader-feed enrichment
already deployed) to the exact reviewed current `main` target
`52c19c3912504412f47e4088e48c9b3b6296ba0c`.

The target adds the accepted PR #60 Mission Control professional cockpit and
data-truth/explainability integration, followed only by documentation
reconciliation commits. Production remains Alpaca Paper.

The first rollout attempt correctly failed closed before mutation because the
canonical docs still described `d14e28d...` as production while the runtime was
actually already at `e113f5c...`. A read-only production inspection then proved:

- HEAD = `e113f5c6255925f1a93f0f8c242dcd5facbaf41a`;
- tracked local delta = only `config/settings.yaml`, with
  `intraday_scan.enabled: true`;
- one untracked rotated runtime log, `quant_agent.log.1`.

The corrected rollout is pinned to that actual baseline. Numeric rotated
`quant_agent.log.N` files are permitted only when they are regular, non-symlink,
`qamc`-owned files. Any other untracked path still fails closed.

The rollout requires the exact baseline plus the authorized intraday override,
pins the target tree and exact 78-file baseline→target file list, refuses an
active QAMC session, runs focused deterministic tests, restarts only Mission
Control, reapplies the authorized intraday override, and verifies providers,
Telegram, timers, read-only API behavior, current quote/history separation and
final production identity. Any failure after checkout attempts convergence back
to the observed baseline plus the same intraday override and restarts Mission
Control.

It never runs a trading mode, never submits/cancels/modifies an order, never
changes timers/services/network/security/OneCLI, and never sends a Telegram
message (`getMe` only).

## Reviewed identities

- Baseline SHA: `e113f5c6255925f1a93f0f8c242dcd5facbaf41a`
- Target SHA: `52c19c3912504412f47e4088e48c9b3b6296ba0c`
- Target tree: `226469380ca38dbcae5c2d31c3b55162ed2f0ed3`
- Changed files: `78`
- Changed-file-list SHA-256: `b56ccf7eeb58ddb9e5a706625a48e4d7c0a91f4a9d1944ef96fb1954e68ed114`
- Rollout-script Git blob: `517de90412e8cb3607add69a26909810fa3bf1e8`
- Rollout-script SHA-256: `8b3d5918dcb08c3acffcffb926b5992014056d6c2a012dafd50cfb5e8570bd3a`

The corrected rollout script was syntax-checked with `bash -n` before
publication; its published Git blob matches the syntax-checked bytes.

## Single operator command

Run this **as `ubuntu` on the VPS**:

```bash
sudo bash -o pipefail -c '[[ "$(stat -c "%U:%G:%a" /root)" == "root:root:700" ]] && sudo -u dev -H git -C /home/dev/projects/quant-agent fetch --no-tags origin "+refs/heads/ops/readside-production-convergence:refs/remotes/origin/ops/readside-production-convergence" && sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 517de90412e8cb3607add69a26909810fa3bf1e8 | install -o root -g root -m 0700 /dev/stdin /root/qamc-readside-rollout.sh && echo "8b3d5918dcb08c3acffcffb926b5992014056d6c2a012dafd50cfb5e8570bd3a  /root/qamc-readside-rollout.sh" | sha256sum -c - && /root/qamc-readside-rollout.sh'
```

The script writes a root-only transcript to
`/root/qamc-readside-rollout-<UTC timestamp>.log`.

Success ends with `FINISH LINE PASSED` and prints the exact production SHA,
local intraday delta, Mission Control/Telegram/timer verdicts and transcript
path. Return that output/transcript for final state reconciliation and the
operator-side desktop/iPad visual check.
