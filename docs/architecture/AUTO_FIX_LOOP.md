# The auto-fix loop — what it is, what it may do, and how to turn it off

**Status as shipped: built, installed by nobody, started by nobody, inert.**
Turning it on is a separate, deliberate act. Nothing in a deploy starts it.

## In one paragraph

Twice each trading day the desk sends a health report — one before it trades,
one after it has stopped. When that report finishes, this starts a single
Claude session that reads the report and the work board, and if it finds one
concrete defect it can prove and fix inside twenty minutes, it fixes it and
opens a pull request. If it finds nothing, it says so and stops. It never
deploys, never trades, never touches money, and never messages the owner.

The owner's ruling that authorises it, in his words: *"the part of plugging you
into the logs that's fine, you can use my cloud allowance, make sure that
you're running everything with adversary."*

## Where the owner sees it

Nowhere, on purpose, unless it does something. It sends no Telegram message of
its own — the desk's alert rule is that every failure alerts in its own
message, and a session that correctly found nothing is not a failure. What it
produces is a pull request, which is where its work is reviewed like anyone
else's. A run that crashed, timed out, or found nothing leaves a line in the
journal and nothing else.

To see what it has been doing:

    systemctl --user status quant-agent-auto-fix.service
    journalctl --user -u quant-agent-auto-fix.service --since today
    gh pr list --search "Opened unattended by the scheduled auto-fix session"

## What starts it

**The report finishing — not a time of day.** This matters and was an explicit
owner correction: a fixed delay after the report's kickoff is a guess about how
long a report takes, and a slow report would be read while it was still being
written.

The chain, in order:

1. `quant-agent-log-health.timer` fires the health report at 08:55 and 16:30 ET,
   Mon-Fri.
2. `scripts/run_log_health_report.sh` runs the report and sends it.
3. **As its last step, after the send returns — success or failure — that same
   script writes `/var/lib/quant-agent/handoff/health-report-complete`.**
4. `quant-agent-auto-fix.path` is watching that file and starts
   `quant-agent-auto-fix.service` the moment it changes.
5. That service runs `scripts/run_auto_fix_session.sh`, which is the whole job.

There is no clock anywhere in steps 3-5.

### Why a marker file instead of the report just calling the script

Because it cannot. The report runs as `qamc`, the trading service account. The
auto-fix session runs as `ubuntu`, the engineering account — that is where the
git checkout, the `gh` credential and the Claude CLI credential live, and
`docs/WORK.md` has said "work as `ubuntu`, never as `qamc`" since long before
this existed. `qamc` has no sudo on this box at all [verified 2026-09-20:
`sudo -l -U qamc` returns "User qamc is not allowed to run sudo"], so it cannot
execute anything as `ubuntu`.

Giving it a sudo rule so it could would hand the account that places orders the
ability to run code as the account that can rewrite the code that places them.
That is a credential-boundary change, which is owner-only, and it is not
needed: a marker file plus an inotify unit fires on exactly the same event with
none of that. The desk already does this — `quant-agent-status-board.path`
watches files under `/home/qamc` from a unit that reacts to them.

The directory must be created once, by hand, owned by the writer:

    sudo install -d -o qamc -g qamc -m 0755 /var/lib/quant-agent/handoff

Until it exists the handoff is a silent no-op and the report behaves exactly as
it did before. That is the shipped state.

## What the session may do

The standing prompt is `config/prompts/auto_fix_session.md` — it is the
contract, and it is the file to edit if the behaviour is wrong. In summary:

- Read the health report snapshot, `docs/WORK.md`, `docs/OUTCOME.md`, and
  `docs/INCIDENT_HISTORY.md`.
- Take **one** defect, and only if it is real, bounded, provable by a test,
  needs no decision that is not the session's to make, and is not already being
  worked on another branch.
- **Run the `qamc-adversary` agent before any change to a rule, a threshold, a
  gate, or an entry/exit path**, and answer its objections in the PR body or
  abandon the change. Unsure counts as yes.
- Open a pull request. A human reviews it, the same as any other.
- Otherwise print `AUTO-FIX: NO ACTION` and stop. **Most runs should end this
  way.** A run that finds nothing is working correctly.

## What it may never do

These are in the prompt so the session understands them, and in
`config/auto_fix_permissions.json` so they hold whether it understands them or
not. Deny rules win over allow rules, so nothing the session reads mid-run — a
log line, a board item, a web page — can talk its way past one.

| Never | Enforced by |
|---|---|
| Enable live trading, or move anything closer to it | prompt; no path to it in the allow list |
| Read, write or reference `.env`, `~/.bashrc`, `~/.claude/**`, or any credential | deny rules |
| Touch anything under `/home/qamc/` | deny rules |
| `sudo`, `systemctl`, `crontab`, `docker`, `chmod`, `chown` | deny rules |
| `git add -A`, `git add .`, bare `git stash` | deny rules |
| `git push --force`, `git reset --hard`, history rewriting | deny rules |
| Deploy (`scripts/merge_and_deploy.sh`), or `gh pr merge --admin` | deny rules |
| Message the owner, or file a `DECIDE BY` line for him | prompt |
| Schedule anything, including another copy of itself | deny rules on `systemctl`/`crontab`/`claude`; prompt |
| More than one fix in a run | prompt |

The session is also started with `--setting-sources local`, so neither the
user's settings nor the project's are loaded. Both would be wrong: the user's
carry broad permission grants made for supervised sessions, and the project's
carry a `Stop` hook that can refuse to let a session end — which, unattended,
means burning the entire time budget arguing with a hook that has nobody to
answer it.

**It opens pull requests; it does not deploy.** On this desk a merge to `main`
is not a deploy — `scripts/merge_and_deploy.sh` is a separate manual step — so
nothing this session does reaches the live desk without a person running that
script.

## What bounds it

- **Time.** `timeout` in the wrapper at 1200s, the same outer kill the trading
  sessions use, with `TimeoutStartSec=1500` on the service behind it. The
  wrapper's kill must land first so its cleanup runs; systemd's is the backstop
  for a wrapper that dies before it can kill its own child. CLI 2.1.275 has no
  `--max-turns`, so wall-clock is the only real cap on the loop — which is why
  there are two of them.
- **Blast radius.** A throwaway git worktree under `/tmp/claude-1000/wt/`,
  created from a freshly-fetched `origin/main` and removed on the way out
  whatever happened, including on a kill.
- **Scope.** One fix attempt per run, stated in the prompt.
- **Reading, not disturbing.** The health snapshot is a re-render with
  `--no-telegram`, which sends nothing and does not advance the report's
  watermark — so this cannot steal the window from the next real report.

## Turning it on

Not yet. In order, when someone decides to:

1. `sudo install -d -o qamc -g qamc -m 0755 /var/lib/quant-agent/handoff`
2. `cp scripts/systemd/engineering/quant-agent-auto-fix.* ~/.config/systemd/user/`
   (as `ubuntu`), then `systemctl --user daemon-reload`
3. **Rehearse, watched:** `scripts/run_auto_fix_session.sh --rehearse`. It does
   all the reading and reasoning and changes nothing. Read what it says it
   would have done. Do this more than once.
4. Only then: `systemctl --user enable --now quant-agent-auto-fix.path`

## Turning it off

    systemctl --user disable --now quant-agent-auto-fix.path

That is the whole kill switch — nothing is watching the marker any more, and
the marker being written becomes a no-op again. Removing the handoff directory
does the same thing from the other end. A session already running is stopped
with `systemctl --user stop quant-agent-auto-fix.service`; its worktree is
removed by its own cleanup.

## Why these units are not in `scripts/systemd/`

`scripts/check_unit_drift.py` compares every file in `scripts/systemd/` against
`/home/qamc/.config/systemd/user` and alerts on anything tracked but not
installed. These units belong to `ubuntu`'s systemd instance, not `qamc`'s, so
they could never appear there and would report as permanent drift — a daily
Telegram alarm that can never be cleared. They live in
`scripts/systemd/engineering/` instead, which that check does not read (it
lists files, not directories).

## Known gaps — honest list

- **It has never run unsupervised.** Every claim here about what it will do
  when it finds a real defect is a claim about a prompt and a permission file,
  not an observation. The only thing proven is the rehearsal path.
- **The permission envelope is unproven against a session that wants to break
  it.** Deny rules are the right mechanism and the list is deliberate, but
  nobody has tried to get past it.
- **No cost cap.** The run is bounded in time, not in spend. Twenty minutes of
  Opus twice a day is the exposure; nothing measures or limits it.
- **No test asserts any of this.** The prompt, the deny list and the handoff
  are all prose and configuration. If someone deletes a deny rule, nothing goes
  red.
- **A failed run is silent** by design, so a session that is broken every time
  looks exactly like a desk with nothing to fix.
