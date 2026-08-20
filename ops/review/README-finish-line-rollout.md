# QAMC finish-line rollout — operator handoff

`qamc-finish-line-rollout.sh` converges production from the pinned Telegram
hotfix to the accepted PR #48 target, enables the already-authorized intraday
opportunity scanner, and runs the governed Stage E acceptance pass — in one
run, in the `docs/WORK.md` order, with every stage gate a fail-closed
checkpoint.

**Do not run it until ChatGPT has approved this branch.**

## Pinned identities

| | |
|---|---|
| Production baseline (rollback point) | `9c736c158fec84129765c25a9429254d3602ad6b` |
| Deployment target | `bb223eadde30654d72ab11e055185a757d0cddc0` |
| Target tree | `ff27c9458ba6f4677c8db2329af7d8d47b176e77` |
| Reviewed script — git blob | `69e0968f6438be0d7d1a5d92d4f0e5e335ba42fe` |
| Reviewed script — sha256 | `77701bb2c41db5c8f7b35b813e83bf46069767d962dfaab1d75aa6bb440bbc10` |
| Reviewed script — size | 75298 bytes |

## The single operator command

Run this **as `ubuntu`** (the only account with sudo):

```bash
sudo bash -o pipefail -c 'sudo -u dev -H git -C /home/dev/projects/quant-agent cat-file blob 69e0968f6438be0d7d1a5d92d4f0e5e335ba42fe | install -o root -g root -m 0700 /dev/stdin /root/qamc-finish-line-rollout.sh && echo "77701bb2c41db5c8f7b35b813e83bf46069767d962dfaab1d75aa6bb440bbc10  /root/qamc-finish-line-rollout.sh" | sha256sum -c - && /root/qamc-finish-line-rollout.sh'
```

Nothing else needs to be typed. The script writes its own complete transcript
to `/root/qamc-rollout-<UTC timestamp>.log` (mode 0600) and prints the path at
the start and the end.

### Why it is shaped like that

Reviewing a file in `/home/dev` and then running *a file at that path* as root
are two different acts, and the Claude Code account can write to `/home/dev` in
between. Every element below closes part of that gap, and the `&&` chain means
any failure stops before execution.

1. **`sudo -u dev -H git … cat-file blob 69e0968f6438…`** reads the reviewed
   bytes out of the content-addressed object store by their own hash. It never
   reads the working tree, so an edited, replaced or symlinked
   `ops/review/qamc-finish-line-rollout.sh` cannot be what gets installed — the
   blob id is the identity ChatGPT reviewed.

   It runs **as `dev`** because `ubuntu` cannot traverse `/home/dev` (mode
   0750, owned by `dev`). The first real run proved this: extraction as
   `ubuntu` fails outright. The outer `sudo` has already made us root, and root
   running `sudo -u dev` needs no password, so this adds no prompt.

2. **`bash -o pipefail`** is load-bearing, not decoration. Without it, a failed
   `git cat-file` still leaves `install` succeeding on empty input, the
   pipeline reports success, and the `&&` chain marches on to install and then
   execute a **zero-byte** file. With `pipefail` the pipeline fails and the
   chain stops. (The sha256 check below is the second line of defence against
   exactly that.)

3. **`install -o root -g root -m 0700 /dev/stdin`** creates the file directly
   as root-owned `0700` — no intermediate unprivileged copy, and no window at
   wider permissions.

4. **`sha256sum -c`** verifies the *installed* bytes, as root, after
   installation. Git object reads do not always re-validate the hash on the way
   out, so this is an independent check of the same content by a different
   algorithm. A mismatch exits non-zero and the chain never reaches execution.

5. **`/root/qamc-finish-line-rollout.sh`** runs only if every step above
   succeeded. The script then re-checks its own identity before acting: owned
   by `root:root`, mode exactly `0700`, in a root-owned directory with no group
   or world write bit.

No secret is read, written, printed or moved by any step, and nothing is passed
on a command line that could reach `ps`.

Residual, stated plainly: this does not defend against an already-root
attacker, who could replace the file between steps 4 and 5. At that point the
host is compromised regardless.

## Before this can run: the target must be re-pinned

**The pinned target `bb223ea` does not contain the corrected listener-privacy
classifier.** Gate B runs the deployed verifier, and Gate E4 loads its
classifier, so on `bb223ea` the run would abort at Gate B on the same false
positive that stopped the first attempt.

The script now detects this in **preflight**, before touching anything, and
stops with instructions. Once ChatGPT merges this branch to `main`, re-pin four
constants at the top of the script and regenerate the provenance hashes:

```bash
git rev-parse origin/main; git rev-parse origin/main^{tree}; git diff --name-only 9c736c158fec84129765c25a9429254d3602ad6b origin/main | wc -l; git diff --name-only 9c736c158fec84129765c25a9429254d3602ad6b origin/main | sha256sum; git hash-object ops/review/qamc-finish-line-rollout.sh; sha256sum ops/review/qamc-finish-line-rollout.sh
```

Nothing else in the script changes; every gate below is unaffected.

## What the script does

```
PHASE 1  PREFLIGHT (Gate A runtime half) — verify everything, change nothing
PHASE 2  FETCH + VERIFY the target commit's CONTENT before checkout
PHASE 3  DEPLOY the pinned SHA + import/config smoke        [MUTATION]
PHASE 4  RESTART Mission Control onto the deployed SHA      [MUTATION]
PHASE 5  GATE B — health, providers, LIVE preflight, Telegram, timers
PHASE 6  GATE C — SGOV funding + Tech batch proven on the DEPLOYED tree
PHASE 7  GATE D — intraday market-data smoke, then enable    [MUTATION]
PHASE 8  GATE E — adversarial end-to-end acceptance (E1–E13)
PHASE 9  FINISH LINE
```

It ends with either `GATE E / FINISH LINE PASSED` or a converged rollback.

**It never** places, cancels or modifies an order; invokes a trading mode or
`main.py`; creates a service, timer, daemon, database or proxy; installs a
package; changes firewall, network or account configuration; modifies OneCLI;
reads, writes, prints or moves a secret value; or sends a Telegram message.
`tests/test_rollout_script.py` asserts all of that statically, and the whole
file contains exactly one broker construction, calling exactly one read-only
method (`get_intraday_snapshots(["SPY"])`).

**One paid call**, explicitly authorized for finish-line acceptance: the
accepted commissioning verifier's `preflight` group runs with `--live` after
deployment, completing one real read per provider — including one OpenRouter
completion per distinct accepted policy model. Every call it makes is
read-only.

## If something goes wrong

Any failure from PHASE 3 onward converges production automatically: config
restored, checkout detached back to the baseline, Mission Control restarted and
proven healthy. The script says which of those succeeded. If convergence is
incomplete it prints `CONVERGENCE INCOMPLETE` with the exact commands to finish
by hand — it never claims a clean rollback it did not achieve.

Manual rollback, if you ever need it:

```bash
sudo -u qamc -H bash -c "cd /home/qamc/quant-agent && git checkout -- config/settings.yaml && git checkout --detach 9c736c158fec84129765c25a9429254d3602ad6b" && sudo -u qamc -H bash -c "export XDG_RUNTIME_DIR=/run/user/1001; export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus; systemctl --user restart quant-agent-api.service" && sleep 5 && curl -s http://127.0.0.1:8800/health
```

To disable intraday only, keeping the deployed code:

```bash
sudo -u qamc -H bash -c "cd /home/qamc/quant-agent && git checkout -- config/settings.yaml"
```

**SIGKILL is the one abort no process can trap.** If the run is `kill -9`ed or
the host dies mid-rollout, do not re-run the script on top: it will detect that
`HEAD` is already at the target, refuse, and print the convergence commands.
Converge first, then re-run.

## What to return for closeout

The whole transcript — `/root/qamc-rollout-*.log`. It contains the deployed
SHA and tree, every gate result, the one-line config delta, and the final
banner. No secret appears in it.

`docs/STATE.md` and `docs/WORK.md` are updated from that transcript, not from
this document; the prepared wording is in
`ops/review/finish-line-closeout-draft.md`.
