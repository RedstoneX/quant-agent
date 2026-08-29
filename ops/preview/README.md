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

> **This is NOT Mission Control, and leaving it running is not harmless.**
> Production Mission Control is `https://ovh-vps.wallaby-bowfin.ts.net/cockpit/`
> — no port — which Tailscale Serve proxies to the qamc API. This preview
> serves whatever is committed in the `dev` checkout, which goes stale the
> moment you stop using it.
>
> On 2026-08-21 an instance started with the `setsid nohup … & disown` recipe
> above was left running for seven days. It kept answering on :8810 with a
> bundle built that morning, so the operator's bookmarked address showed a
> cockpit predating PR #120 while three merged, deployed passes of cockpit
> work were invisible — read as "the deploy didn't work" rather than "the
> URL is wrong". **Kill it when your review session ends. Every time.**

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
production for real data (deliberately, rather than faking it), those
fields/endpoints are missing from the raw upstream response.

As of the Stage 6e fix, `branch_preview.py` reconstructs the missing
`liquidity`/`direction` fields and the `/runs/{id}/funnel` endpoint
itself from real data upstream **does** already expose (`/account`,
`/positions`, `/runs/{id}`, `/runs/{id}/candidates(/{symbol})`), using
the same typed schemas and pure derivation rules this branch's own
backend uses — see the module docstring and `_reconstruct_funnel`'s
docstring in `branch_preview.py` for exactly what is reconstructed and
from where. The decision funnel, directional bias, cash/liquidity
breakdown, and position-direction panels now render real production data
in this preview — see `docs/verification/stage-6e-preview-contract-fix/`.
Reconstruction is self-obsoleting: once this branch merges and `qamc`
production actually returns these fields/routes, the real upstream value
passes straight through untouched.

The one exception is `GET /prices/{symbol}` (the price chart panel),
which still shows an honest "could not load" state in this preview —
real OHLCV bars require Alpaca market-data credentials this `dev`-account
process deliberately does not have, so there is no real data to
reconstruct from. The chart component's own correctness against real
bars was already verified separately with seeded data — see
`docs/verification/stage-6-react/` (and
`docs/verification/stage-6b-deliberation-journal-bias/` /
`docs/verification/stage-6c-directional-exposure-fix/` for the other
panels' pre-existing seeded-data verification).

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
