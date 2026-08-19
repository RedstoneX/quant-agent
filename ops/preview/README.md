# Branch preview — operator review before merge

Lets the operator open the **current feature branch's** Mission Control
frontend in a browser, from any tailnet device, before it is merged —
without touching the `qamc` production runtime in any way.

## What it is

`branch_preview.py` is a small, ephemeral, read-only FastAPI process that:

- serves this `dev` checkout's `/ui` (legacy dashboard) and `/cockpit`
  (React cockpit) static bundles exactly as committed on the current
  branch;
- proxies every other GET request (the JSON API) to the real, already-
  running `qamc` production Mission Control API on `127.0.0.1:8800`, so
  the operator sees genuinely truthful account/position/trade/journal
  data — never duplicated, never faked;
- enforces GET-only itself, before forwarding anything upstream;
- binds only to an explicit interface address you pass — normally this
  VPS's Tailscale IP, never `0.0.0.0`, never the public interface.

It is **not** a new durable service: no systemd unit, no auto-start. Start
it for a review session, stop it when done.

## Start

```bash
cd /home/dev/projects/quant-agent
source .venv/bin/activate
setsid nohup python ops/preview/branch_preview.py --host 100.111.170.97 --port 8810 \
  > /tmp/branch_preview.log 2>&1 < /dev/null &
disown
```

(`100.111.170.97` is this VPS's Tailscale IP as of 2026-08-19 — confirm
with `tailscale ip -4` or `ip addr show tailscale0` if it's ever changed.)

Open from any other tailnet device:

- `http://ovh-vps.wallaby-bowfin.ts.net:8810/cockpit/` — the new React cockpit
- `http://ovh-vps.wallaby-bowfin.ts.net:8810/ui/` — the legacy dashboard (fallback, unchanged)

## Stop

```bash
pkill -f "ops/preview/branch_preview.py"
```

or `kill <pid>` for the specific PID `ps aux | grep branch_preview` shows.

## Known limitation — version skew against current production

This branch adds new backend endpoints (`GET /runs/{id}/funnel`,
`GET /prices/{symbol}`, `liquidity`/`direction` fields on `/account` and
`/positions`) that are **not yet deployed to `qamc` production**, because
this branch hasn't been merged/deployed yet. Since the preview proxies to
production for real data (deliberately, rather than faking it), panels
that depend on those new endpoints — the decision funnel, directional
bias, and per-run journal narrative — will show an honest "could not
load" state rather than data, and the browser console will show the
corresponding failed-request entries. This is expected, not a defect:
every other panel (account, positions, orders, trades, runs, evening
reflection, missed opportunities, search) renders real production data
correctly. The new panels' actual correctness against a version-matched
backend was already verified separately — see
`docs/verification/stage-6b-deliberation-journal-bias/` and
`docs/verification/stage-6c-directional-exposure-fix/` (seeded,
representative data, zero console errors).

## Safety boundaries (verified, not just asserted)

- Binding to a specific non-`0.0.0.0` address means the OS itself never
  accepts a connection arriving on any other interface — confirmed
  unreachable via this VPS's public IP and even via `127.0.0.1`.
- GET-only enforced in this process's own middleware; POST/DELETE/etc.
  return 405 before anything is forwarded upstream.
- Never opens, reads, or writes any `qamc`-owned file or credential —
  the `dev`/`qamc` account boundary is unaffected (confirmed: `dev` has
  no filesystem permission into `/home/qamc/` at all).
- Never sends `qamc` a write request — it only ever issues GETs to
  `127.0.0.1:8800`, the same read-only surface any browser already uses.
- Never starts, stops, restarts, or otherwise manages the `qamc` systemd
  service — confirmed via PID/uptime before and after use.
