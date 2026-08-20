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
| Reviewed script — git blob | `b24ffacaa98b664b215db2b882eddda185b78d4e` |
| Reviewed script — sha256 | `9870c402fd8047b01d1e419b2155b149680ce829979ef5c0813e51d111fe1509` |
| Reviewed script — size | 72275 bytes |

## The single operator command

```bash
git -C /home/dev/projects/quant-agent cat-file blob b24ffacaa98b664b215db2b882eddda185b78d4e | sudo install -o root -g root -m 0700 /dev/stdin /root/qamc-finish-line-rollout.sh && echo "9870c402fd8047b01d1e419b2155b149680ce829979ef5c0813e51d111fe1509  /root/qamc-finish-line-rollout.sh" | sudo sha256sum -c - && sudo /root/qamc-finish-line-rollout.sh
```

Nothing else needs to be typed. The script writes its own complete transcript
to `/root/qamc-rollout-<UTC timestamp>.log` (mode 0600) and prints the path at
the start and the end.

### Why it is shaped like that

The problem this replaces is a real one: reviewing a file in `/home/dev` and
then running *a file at that path* as root are two different acts, and the
Claude Code account can write to `/home/dev` in between. Every step below
closes part of that gap, and the chain is `&&`-joined so any failure stops
before execution.

1. **`git cat-file blob <blob>`** reads the reviewed bytes out of the
   content-addressed object store by their own hash. It does not read the
   working tree, so an edited, replaced or symlinked
   `ops/review/qamc-finish-line-rollout.sh` cannot be what gets installed. The
   blob id is the identity ChatGPT reviewed.
2. **`sudo install -o root -g root -m 0700 /dev/stdin`** creates the file
   directly as root-owned `0700` in `/root`. There is no intermediate
   unprivileged copy, and no window where the file exists with wider
   permissions.
3. **`sha256sum -c`** verifies the *installed* bytes against the reviewed
   hash, as root, after installation. Git object reads do not always
   re-validate the hash on the way out, so this is an independent check of the
   same content by a different algorithm. A mismatch exits non-zero and the
   `&&` chain never reaches execution.
4. **`sudo /root/qamc-finish-line-rollout.sh`** runs the verified file. The
   script then re-checks its own identity before doing anything: it refuses
   unless it is owned by `root:root`, is mode exactly `0700` (not merely
   "no write bits for others"), and sits in a root-owned directory that is not
   group- or world-writable.

No secret is read, written, printed or moved by any step, and nothing here is
passed on a command line that could reach `ps`.

Residual, stated plainly: this does not defend against an already-root
attacker, who could replace the file between step 3 and step 4. At that point
the host is compromised regardless.

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
